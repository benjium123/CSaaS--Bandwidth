package main

import (
	"sync"
	"time"

	"github.com/pion/rtp"
)

// packetWriter is satisfied by *oggwriter.OggWriter (pion/webrtc/v4/pkg/media/oggwriter).
type packetWriter interface {
	WriteRTP(p *rtp.Packet) error
	Close() error
}

const (
	samplesPerMs   = 48               // Opus RTP clock is 48 kHz
	silenceSamples = 960              // one 20 ms silence frame
	maxFillSamples = 4 * 3600 * 48000 // never fill a gap longer than 4 h (jump instead)
	tsOffset       = 48000            // written timestamp = tsOffset + position (keeps clear of oggwriter's sentinel value 1)
)

// silenceFrame is a valid 20 ms Opus CELT fullband silence frame (TOC 0xF8 + 0xFF 0xFE).
var silenceFrame = []byte{0xF8, 0xFF, 0xFE}

// opusSamples returns the number of 48 kHz samples in one Opus packet, parsed from its TOC byte
// (RFC 6716 section 3.1). config = toc>>3: 0-11 SILK frame 10/20/40/60 ms by config%4;
// 12-15 Hybrid 10/20 ms by config%2; 16-31 CELT 2.5/5/10/20 ms by config%4.
// Frame count by toc&3: 0 -> 1, 1 -> 2, 2 -> 2, 3 -> payload[1]&0x3F (needs len>=2).
// Return 960 when the payload is empty/unparsable or the result is 0.
func opusSamples(payload []byte) uint32 {
	if len(payload) == 0 {
		return 960
	}

	toc := payload[0]
	config := toc >> 3

	var frames uint32
	switch toc & 3 {
	case 0:
		frames = 1
	case 1, 2:
		frames = 2
	case 3:
		if len(payload) < 2 {
			return 960
		}
		frames = uint32(payload[1] & 0x3F)
	}

	var samplesPerFrame uint32
	switch {
	case config <= 11:
		switch config % 4 {
		case 0:
			samplesPerFrame = 480 // 10 ms
		case 1:
			samplesPerFrame = 960 // 20 ms
		case 2:
			samplesPerFrame = 1920 // 40 ms
		case 3:
			samplesPerFrame = 2880 // 60 ms
		}
	case config <= 15:
		switch config % 2 {
		case 0:
			samplesPerFrame = 480 // 10 ms
		case 1:
			samplesPerFrame = 960 // 20 ms
		}
	default:
		switch config % 4 {
		case 0:
			samplesPerFrame = 120 // 2.5 ms
		case 1:
			samplesPerFrame = 240 // 5 ms
		case 2:
			samplesPerFrame = 480 // 10 ms
		case 3:
			samplesPerFrame = 960 // 20 ms
		}
	}

	if samplesPerFrame == 0 || frames == 0 {
		return 960
	}
	return samplesPerFrame * frames
}

// sideWriter owns one side's output file. Safe for concurrent use (two tracks of the same
// participant can overlap briefly while an old track drains).
type sideWriter struct {
	mu        sync.Mutex
	w         packetWriter
	roomStart time.Time
	end       uint32 // next free position, samples since roomStart
	seq       uint16
	written   int64
	silence   int64
	dropped   int64
}

// newSideWriter creates a sideWriter that writes to w starting at roomStart.
func newSideWriter(w packetWriter, roomStart time.Time) *sideWriter {
	return &sideWriter{w: w, roomStart: roomStart}
}

// sideTrack is one RTP track feeding a sideWriter.
type sideTrack struct {
	s       *sideWriter
	base    uint32
	firstTS uint32
	started bool
}

// beginTrack starts a new track whose first packet arrives at wall time now.
// base = max(s.end, samples elapsed from roomStart to now) (elapsed<0 -> 0; ms * samplesPerMs).
func (s *sideWriter) beginTrack(now time.Time) *sideTrack {
	s.mu.Lock()
	defer s.mu.Unlock()

	elapsedMs := now.Sub(s.roomStart).Milliseconds()
	if elapsedMs < 0 {
		elapsedMs = 0
	}

	wall := uint32(elapsedMs * samplesPerMs)
	if wall < s.end {
		wall = s.end
	}

	return &sideTrack{s: s, base: wall}
}

// write takes packets of ONE track, already in sequence order.
//   - first packet: firstTS = p.Timestamp.
//   - pos = t.base + (p.Timestamp - t.firstTS)   (uint32 arithmetic: RTP timestamp wrap is safe)
//   - if pos < s.end: overlapping/late -> dropped++, return nil.
//   - if pos > s.end and pos-s.end <= maxFillSamples: write silence frames at s.end while
//     s.end+silenceSamples <= pos (each: s.end += 960, silence++). A remainder < 960 is left as a
//     small jump. If the gap is > maxFillSamples, the track is re-anchored at s.end (RTP clock reset).
//   - write a COPY of the packet (do not mutate the caller's packet): Header copied, Timestamp =
//     tsOffset+pos, SequenceNumber = s.seq (then s.seq++), Payload shared.
//   - s.end = pos + opusSamples(p.Payload); written++.
//   - Return the writer's error, if any (still advance nothing on error).
func (t *sideTrack) write(p *rtp.Packet) error {
	s := t.s
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.w == nil { // closed: a draining track may still deliver a packet
		s.dropped++
		return nil
	}

	if !t.started {
		t.firstTS = p.Header.Timestamp
		t.started = true
	}

	pos := t.base + (p.Header.Timestamp - t.firstTS)

	if pos < s.end {
		s.dropped++
		return nil
	}

	if pos-s.end > maxFillSamples {
		// The sender reset its RTP clock (or the gap is absurd): re-anchor this track at
		// the current end of the file instead of jumping hours ahead.
		t.base, t.firstTS, pos = s.end, p.Header.Timestamp, s.end
	}
	for s.end+silenceSamples <= pos {
		if err := s.writeSilenceLocked(); err != nil {
			return err
		}
	}

	hdr := p.Header
	hdr.Timestamp = tsOffset + pos
	hdr.SequenceNumber = s.seq
	out := &rtp.Packet{Header: hdr, Payload: p.Payload}

	if err := s.w.WriteRTP(out); err != nil {
		return err
	}

	s.seq++
	s.end = pos + opusSamples(p.Payload)
	s.written++
	return nil
}

// writeSilenceLocked is the helper for one silence frame at s.end (caller holds the lock).
func (s *sideWriter) writeSilenceLocked() error {
	pkt := &rtp.Packet{
		Header: rtp.Header{
			Version:        2,
			SequenceNumber: s.seq,
			Timestamp:      tsOffset + s.end,
		},
		Payload: silenceFrame,
	}

	if err := s.w.WriteRTP(pkt); err != nil {
		return err
	}

	s.seq++
	s.end += silenceSamples
	s.silence++
	return nil
}

// close closes the underlying writer (once; later calls return nil).
func (s *sideWriter) close() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.w == nil {
		return nil
	}

	err := s.w.Close()
	s.w = nil
	return err
}

// stats returns counters and the timeline length in ms (end / samplesPerMs).
func (s *sideWriter) stats() (written, silence, dropped, durationMs int64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	return s.written, s.silence, s.dropped, int64(s.end / samplesPerMs)
}
