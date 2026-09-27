package main

import (
	"errors"
	"math"
	"math/rand"
	"testing"
	"time"
)

func feedSamples(g *speechGate, start time.Time, chunk int, samples []int16) time.Time {
	now := start
	for beg := 0; beg < len(samples); beg += chunk {
		end := beg + chunk
		if end > len(samples) {
			end = len(samples)
		}
		now = now.Add(time.Duration(end-beg) * time.Second / gateRate)
		g.feedPCM(samples[beg:end], now)
	}
	return now
}

func isHeard(g *speechGate) bool {
	select {
	case <-g.heard():
		return true
	default:
		return false
	}
}

func assertHeard(t *testing.T, g *speechGate) {
	t.Helper()
	if !isHeard(g) {
		t.Fatal("expected heard channel to be closed")
	}
}

func assertNotHeard(t *testing.T, g *speechGate) {
	t.Helper()
	if isHeard(g) {
		t.Fatal("heard channel unexpectedly closed")
	}
}

func ringback(ms int) []int16 {
	n := ms * gateRate / 1000
	out := make([]int16, n)
	for i := 0; i < n; i++ {
		t := float64(i) / gateRate
		v := 0.25 * 32767 * (math.Sin(2*math.Pi*440*t) + math.Sin(2*math.Pi*480*t)) / 2
		out[i] = int16(v)
	}
	return out
}

func sine(ms int, freq float64) []int16 {
	n := ms * gateRate / 1000
	out := make([]int16, n)
	for i := 0; i < n; i++ {
		t := float64(i) / gateRate
		out[i] = int16(0.25 * 32767 * math.Sin(2*math.Pi*freq*t))
	}
	return out
}

func silence(ms int) []int16 {
	n := ms * gateRate / 1000
	rng := rand.New(rand.NewSource(13))
	out := make([]int16, n)
	for i := 0; i < n; i++ {
		v := rng.NormFloat64() * 20
		if v > 32767 {
			v = 32767
		} else if v < -32768 {
			v = -32768
		}
		out[i] = int16(v)
	}
	return out
}

func voice(ms int) []int16 {
	n := ms * gateRate / 1000
	rng := rand.New(rand.NewSource(7))
	raw := make([]float64, n)
	phase := 0.0
	peak := 0.0
	for i := 0; i < n; i++ {
		t := float64(i) / gateRate
		progress := float64(i) / float64(n-1)
		f0 := 110 + 50*progress
		phase += 2 * math.Pi * f0 / gateRate

		s := 0.0
		for k := 1; k <= 25; k++ {
			freq := f0 * float64(k)
			amp := 1.0 / float64(k)
			boost1 := 3.0 * math.Exp(-((freq-700)*(freq-700))/(2*80*80))
			boost2 := 3.0 * math.Exp(-((freq-1200)*(freq-1200))/(2*120*120))
			amp *= 1 + boost1 + boost2
			s += amp * math.Sin(float64(k)*phase)
		}

		env := 0.75 + 0.25*math.Sin(2*math.Pi*4*t)
		s *= env
		raw[i] = s
		if math.Abs(s) > peak {
			peak = math.Abs(s)
		}
	}

	out := make([]int16, n)
	scale := 0.0
	if peak > 0 {
		scale = 0.3 / peak
	}
	for i, v := range raw {
		val := v*scale*32767 + rng.NormFloat64()*0.05*32767
		if val > 32767 {
			val = 32767
		} else if val < -32768 {
			val = -32768
		}
		out[i] = int16(val)
	}
	return out
}

func TestRingbackNeverTriggers(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)
	g.activate(t0)

	now := t0
	now = feedSamples(g, now, gateFrame, ringback(2000))
	now = feedSamples(g, now, gateFrame, silence(2000))
	now = feedSamples(g, now, gateFrame, ringback(2000))
	assertNotHeard(t, g)
}

func TestSilenceNeverTriggers(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)
	g.activate(t0)
	feedSamples(g, t0, gateFrame, silence(6000))
	assertNotHeard(t, g)
}

func TestHelloThenPauseTriggers(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)
	g.activate(t0)

	now := feedSamples(g, t0, gateFrame, silence(600))
	now = feedSamples(g, now, gateFrame, voice(500))
	assertNotHeard(t, g)

	feedSamples(g, now, gateFrame, silence(400))
	assertHeard(t, g)
}

func TestContinuousSpeechCapsAtMaxWait(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)
	g.activate(t0)

	voiceSamples := voice(3000)
	now := t0
	var heardAt time.Time

loop:
	for start := 0; start < len(voiceSamples); start += gateFrame {
		end := start + gateFrame
		if end > len(voiceSamples) {
			end = len(voiceSamples)
		}
		now = now.Add(time.Duration(end-start) * time.Second / gateRate)
		g.feedPCM(voiceSamples[start:end], now)
		if isHeard(g) {
			heardAt = now
			break loop
		}
	}

	if heardAt.IsZero() {
		t.Fatal("heard channel was not closed for continuous speech")
	}
	if heardAt.After(t0.Add(1700 * time.Millisecond)) {
		t.Fatalf("heard fired too late: at %v, want <= %v", heardAt, t0.Add(1700*time.Millisecond))
	}
}

func TestAudioBeforeActivateIgnored(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)

	now := feedSamples(g, t0, gateFrame, voice(1000))
	now = feedSamples(g, now, gateFrame, silence(400))
	assertNotHeard(t, g)

	g.activate(now)
	feedSamples(g, now, gateFrame, silence(1000))
	assertNotHeard(t, g)
}

func TestIsSpeechFrame(t *testing.T) {
	if isSpeechFrame(ringback(20)) {
		t.Fatal("ringback frame should be classified as tonal, not speech")
	}
	if isSpeechFrame(sine(20, 1000)) {
		t.Fatal("1000 Hz sine frame should be classified as tonal, not speech")
	}
	if isSpeechFrame(silence(20)) {
		t.Fatal("silence frame should be classified as quiet, not speech")
	}

	v := voice(500)
	frame := make([]int16, gateFrame)
	copy(frame, v[2*gateFrame:3*gateFrame])
	if !isSpeechFrame(frame) {
		t.Fatal("voice frame should be classified as speech")
	}
}

func TestFeedPCMOddChunkSizes(t *testing.T) {
	g := newSpeechGate(nil)
	t0 := time.Unix(0, 0)
	g.activate(t0)

	now := feedSamples(g, t0, 137, voice(600))
	feedSamples(g, now, 137, silence(400))
	assertHeard(t, g)
}

type fakeDecoder struct {
	voice   []int16
	silence []int16
}

func (d *fakeDecoder) Decode(packet []byte, pcm []int16) (int, error) {
	if len(packet) != 1 {
		return 0, errors.New("unexpected packet")
	}
	switch packet[0] {
	case 1:
		return 0, errors.New("bad packet")
	case 2:
		return copy(pcm, d.voice), nil
	case 3:
		return copy(pcm, d.silence), nil
	default:
		return 0, errors.New("unexpected packet")
	}
}

func TestFeedOpusUsesDecoderAndSkipsErrors(t *testing.T) {
	v := voice(500)
	dec := &fakeDecoder{
		voice:   v[2*gateFrame : 3*gateFrame], // mid-syllable, as in TestIsSpeechFrame
		silence: silence(20),
	}
	g := newSpeechGate(dec)
	t0 := time.Unix(0, 0)
	g.activate(t0)

	now := t0
	g.feedOpus([]byte{1}, now.Add(20*time.Millisecond)) // error packet, ignored
	for i := 0; i < 25; i++ {
		now = now.Add(20 * time.Millisecond)
		g.feedOpus([]byte{2}, now) // voice packet
	}
	g.feedOpus([]byte{1}, now.Add(20*time.Millisecond)) // error packet, ignored
	now = now.Add(20 * time.Millisecond)
	for i := 0; i < 20; i++ {
		now = now.Add(20 * time.Millisecond)
		g.feedOpus([]byte{3}, now) // silence packet
	}

	assertHeard(t, g)
}
