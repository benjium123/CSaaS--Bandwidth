package main

import (
	"log"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

// trackStats is returned when a track finishes.
type trackStats struct {
	Packets    int64
	Reordered  int64 // arrived out of order, put back in order
	Dropped    int64 // duplicates, too-late packets, skipped gaps
	PreConsent int64 // arrived before the announcement finished; never written
}

// reorderBuf is a tiny RTP reorder buffer. Packets are released strictly in
// sequence order; a gap that is still open once the buffer is full is skipped.
// Sequence numbers wrap at 65535, so all comparisons use int16(a-b).
type reorderBuf struct {
	size    int
	started bool
	next    uint16 // next sequence number to release
	pending map[uint16]*rtp.Packet
	stats   *trackStats
}

func newReorderBuf(size int, stats *trackStats) *reorderBuf {
	return &reorderBuf{size: size, pending: make(map[uint16]*rtp.Packet, size), stats: stats}
}

func (b *reorderBuf) push(p *rtp.Packet) []*rtp.Packet {
	seq := p.SequenceNumber
	if !b.started {
		b.started, b.next = true, seq
	}
	if int16(seq-b.next) < 0 { // already released or skipped
		b.stats.Dropped++
		return nil
	}
	if _, dup := b.pending[seq]; dup {
		b.stats.Dropped++
		return nil
	}
	if seq != b.next {
		b.stats.Reordered++
	}
	b.pending[seq] = p
	out := b.drain()
	if len(b.pending) >= b.size { // gap did not fill in time: skip to the lowest buffered seq
		low := b.lowest()
		b.stats.Dropped += int64(uint16(low - b.next))
		b.next = low
		out = append(out, b.drain()...)
	}
	return out
}

// drain releases consecutive packets starting at b.next.
func (b *reorderBuf) drain() []*rtp.Packet {
	var out []*rtp.Packet
	for {
		p, ok := b.pending[b.next]
		if !ok {
			return out
		}
		delete(b.pending, b.next)
		out = append(out, p)
		b.next++
	}
}

func (b *reorderBuf) lowest() uint16 {
	first := true
	var low uint16
	for s := range b.pending {
		if first || int16(s-low) < 0 {
			low, first = s, false
		}
	}
	return low
}

// flush releases everything still buffered, in order, skipping gaps.
func (b *reorderBuf) flush() []*rtp.Packet {
	var out []*rtp.Packet
	for len(b.pending) > 0 {
		b.next = b.lowest()
		out = append(out, b.drain()...)
	}
	return out
}

// recordTrack copies one track's Opus packets into its participant's side file
// until the track ends. Audio is never decoded. Nothing is written before the
// room is armed (announcement played); the side file and this track's place on
// the shared timeline are created by the first packet written.
func recordTrack(track *webrtc.TrackRemote, rr *roomRec, identity, kind string) {
	var st trackStats
	var seg *sideTrack
	buf := newReorderBuf(8, &st)
	write := func(ps []*rtp.Packet) {
		for _, p := range ps {
			if !rr.armed.Load() {
				st.PreConsent++
				continue
			}
			if seg == nil {
				sw, err := rr.side(identity, kind)
				if err != nil {
					log.Printf("side open failed room=%s identity=%s err=%v", rr.name, identity, err)
					st.Dropped++
					continue
				}
				seg = sw.beginTrack(time.Now())
			}
			if err := seg.write(p); err != nil {
				st.Dropped++
				continue
			}
			st.Packets++
		}
	}
	for {
		p, _, err := track.ReadRTP()
		if err != nil {
			break
		}
		if len(p.Payload) == 0 {
			continue
		}
		write(buf.push(p))
	}
	write(buf.flush())
	log.Printf("track ended room=%s identity=%s packets=%d reordered=%d dropped=%d pre_consent=%d",
		rr.name, identity, st.Packets, st.Reordered, st.Dropped, st.PreConsent)
}
