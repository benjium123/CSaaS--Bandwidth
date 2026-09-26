package main

import (
	"testing"
	"time"

	"github.com/pion/rtp"
)

// celt20ms is a minimal Opus payload whose TOC (0xF8) announces one CELT fullband 20 ms frame,
// so opusSamples(celt20ms) == 960.
var celt20ms = []byte{0xF8, 1, 2}

// fakeWriter records every packet handed to it and counts Close calls.
type fakeWriter struct {
	pkts   []rtp.Packet
	closed int
}

func (f *fakeWriter) WriteRTP(p *rtp.Packet) error {
	f.pkts = append(f.pkts, *p)
	return nil
}

func (f *fakeWriter) Close() error {
	f.closed++
	return nil
}

// pkt builds an RTP packet for tests.
func pkt(seq uint16, ts uint32, payload []byte) *rtp.Packet {
	return &rtp.Packet{
		Header:  rtp.Header{Version: 2, SequenceNumber: seq, Timestamp: ts},
		Payload: payload,
	}
}

func checkStats(t *testing.T, sw *sideWriter, written, silence, dropped int64) {
	t.Helper()
	w, s, d, _ := sw.stats()
	if w != written || s != silence || d != dropped {
		t.Fatalf("stats = written %d silence %d dropped %d, want %d %d %d", w, s, d, written, silence, dropped)
	}
}

func checkTS(t *testing.T, got uint32, want int) {
	t.Helper()
	if got != uint32(want) {
		t.Fatalf("timestamp = %d, want %d", got, want)
	}
}

func TestOpusSamples(t *testing.T) {
	cases := []struct {
		name    string
		payload []byte
		want    uint32
	}{
		{"celt 20ms 1 frame", []byte{0xF8}, 960},
		{"celt 20ms 2 frames", []byte{0xF9}, 1920},
		{"silk 20ms config 1", []byte{0x08}, 960},
		{"silk 60ms config 3", []byte{0x18}, 2880},
		{"hybrid 10ms config 12", []byte{0x60}, 480},
		{"celt 2.5ms config 16", []byte{0x80}, 120},
		{"code 3 frame count", []byte{0xFB, 0x03}, 3 * 960},
		{"empty payload", nil, 960},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := opusSamples(tc.payload); got != tc.want {
				t.Fatalf("opusSamples(% x) = %d, want %d", tc.payload, got, tc.want)
			}
		})
	}
}

func TestContinuousNoSilence(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	for i := 0; i < 50; i++ {
		if err := tr.write(pkt(uint16(i), uint32(i*960), celt20ms)); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
	}
	checkStats(t, sw, 50, 0, 0)
	for i, p := range f.pkts {
		checkTS(t, p.Timestamp, tsOffset+i*960)
	}
	_, _, _, durationMs := sw.stats()
	if durationMs != 1000 {
		t.Fatalf("durationMs = %d, want 1000", durationMs)
	}
}

func TestLeadingSilence(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart.Add(100 * time.Millisecond))
	if err := tr.write(pkt(0, 4242, celt20ms)); err != nil {
		t.Fatalf("write: %v", err)
	}
	checkStats(t, sw, 1, 5, 0)
	if len(f.pkts) != 6 {
		t.Fatalf("fake writer got %d packets, want 6", len(f.pkts))
	}
	for i := 0; i < 5; i++ {
		checkTS(t, f.pkts[i].Timestamp, tsOffset+i*960)
		if len(f.pkts[i].Payload) != len(silenceFrame) || f.pkts[i].Payload[0] != silenceFrame[0] {
			t.Fatalf("packet %d payload = % x, want silence frame % x", i, f.pkts[i].Payload, silenceFrame)
		}
	}
	checkTS(t, f.pkts[5].Timestamp, tsOffset+4800)
}

func TestDTXGapFilled(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	for i, ts := range []uint32{0, 960, 960 * 21} {
		if err := tr.write(pkt(uint16(i), ts, celt20ms)); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
	}
	checkStats(t, sw, 3, 19, 0)
	for i := 1; i < len(f.pkts); i++ {
		if f.pkts[i].Timestamp != f.pkts[i-1].Timestamp+silenceSamples {
			t.Fatalf("packet %d ts = %d, want %d (prev %d + 960)", i, f.pkts[i].Timestamp, f.pkts[i-1].Timestamp+960, f.pkts[i-1].Timestamp)
		}
	}
}

func TestRepublishContinues(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	a := sw.beginTrack(roomStart)
	for i := 0; i < 10; i++ {
		if err := a.write(pkt(uint16(i), uint32(i*960), celt20ms)); err != nil {
			t.Fatalf("track A write %d: %v", i, err)
		}
	}
	b := sw.beginTrack(roomStart.Add(2 * time.Second))
	if err := b.write(pkt(0, 3_000_000_000, celt20ms)); err != nil {
		t.Fatalf("track B write: %v", err)
	}
	checkStats(t, sw, 11, 90, 0)
	if len(f.pkts) != 101 {
		t.Fatalf("fake writer got %d packets, want 101", len(f.pkts))
	}
	checkTS(t, f.pkts[10].Timestamp, tsOffset+9600)
	checkTS(t, f.pkts[len(f.pkts)-1].Timestamp, tsOffset+96000)
}

func TestOverlapDropped(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	a := sw.beginTrack(roomStart)
	for i := 0; i < 50; i++ {
		if err := a.write(pkt(uint16(i), uint32(i*960), celt20ms)); err != nil {
			t.Fatalf("track A write %d: %v", i, err)
		}
	}
	b := sw.beginTrack(roomStart.Add(500 * time.Millisecond))
	if b.base != 48000 {
		t.Fatalf("track B base = %d, want 48000 (end, not the 24000 wall position)", b.base)
	}
	before := len(f.pkts)
	if err := a.write(pkt(50, 47040, celt20ms)); err != nil { // pos 47040 < end 48000
		t.Fatalf("late write: %v", err)
	}
	checkStats(t, sw, 50, 0, 1)
	if len(f.pkts) != before {
		t.Fatalf("fake writer got %d packets after late write, want %d", len(f.pkts), before)
	}
}

func TestTimestampWrap(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	first := uint32(0xFFFFFC40) // var, so first+960 wraps at run time
	if err := tr.write(pkt(0, first, celt20ms)); err != nil {
		t.Fatalf("write 1: %v", err)
	}
	if err := tr.write(pkt(1, first+960, celt20ms)); err != nil { // wraps to 0x00000000
		t.Fatalf("write 2: %v", err)
	}
	checkStats(t, sw, 2, 0, 0)
	checkTS(t, f.pkts[0].Timestamp, tsOffset+0)
	checkTS(t, f.pkts[1].Timestamp, tsOffset+960)
}

func TestClockResetReanchored(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	if err := tr.write(pkt(0, 1000, celt20ms)); err != nil {
		t.Fatalf("write 1: %v", err)
	}
	if err := tr.write(pkt(1, 1000+5*3600*48000, celt20ms)); err != nil { // 5 h ahead, > maxFillSamples
		t.Fatalf("write 2: %v", err)
	}
	checkStats(t, sw, 2, 0, 0)
	checkTS(t, f.pkts[0].Timestamp, tsOffset+0)
	checkTS(t, f.pkts[1].Timestamp, tsOffset+960)
}

func TestCallerPacketNotMutated(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	in := pkt(7, 12345, celt20ms)
	if err := tr.write(in); err != nil {
		t.Fatalf("write: %v", err)
	}
	if in.Header.Timestamp != 12345 || in.Header.SequenceNumber != 7 {
		t.Fatalf("caller packet mutated: ts %d seq %d, want 12345 7", in.Header.Timestamp, in.Header.SequenceNumber)
	}
	checkStats(t, sw, 1, 0, 0)
	checkTS(t, f.pkts[0].Timestamp, tsOffset+0)
	if f.pkts[0].SequenceNumber != 0 {
		t.Fatalf("output sequence = %d, want 0", f.pkts[0].SequenceNumber)
	}
}

func TestWriteAfterClose(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	tr := sw.beginTrack(roomStart)
	if err := sw.close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := tr.write(pkt(0, 0, celt20ms)); err != nil {
		t.Fatalf("write after close: %v", err)
	}
	if len(f.pkts) != 0 {
		t.Fatalf("fake writer got %d packets after close, want 0", len(f.pkts))
	}
	checkStats(t, sw, 0, 0, 1)
}

func TestCloseOnce(t *testing.T) {
	roomStart := time.Now()
	f := &fakeWriter{}
	sw := newSideWriter(f, roomStart)
	if err := sw.close(); err != nil {
		t.Fatalf("close 1: %v", err)
	}
	if err := sw.close(); err != nil {
		t.Fatalf("close 2: %v", err)
	}
	if f.closed != 1 {
		t.Fatalf("fake writer closed = %d, want 1", f.closed)
	}
}
