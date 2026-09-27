package main

import (
	"math"
	"sort"
	"sync"
	"time"
)

// pcmDecoder decodes one Opus packet into 16 kHz mono int16 PCM; returns samples written.
type pcmDecoder interface {
	Decode(packet []byte, pcm []int16) (int, error)
}

const (
	gateRate        = 16000
	gateFrame       = 320  // 20 ms at 16 kHz
	gateMinSpeechMS = 200  // speech needed (counted in voiced frames) to consider someone talking
	gateQuietMS     = 300  // after speech, this much quiet = they finished "Hello?" -> fire
	gateMaxWaitMS   = 1500 // after speech first detected, fire at the latest this much later

	talkStartFrames = gateMinSpeechMS * gateRate / (1000 * gateFrame)
	quietFrames     = gateQuietMS * gateRate / (1000 * gateFrame)
)

// speechGate is safe for concurrent use: feeding is expected from one
// goroutine, while heard/activate may be called from other goroutines.
type speechGate struct {
	mu      sync.Mutex
	dec     pcmDecoder
	heardCh chan struct{}

	activated bool
	fired     bool

	buf []int16

	hist        [talkStartFrames * 2]bool // last 400 ms of voiced frame classifications
	histIdx     int
	voicedCount int

	talkStarted bool
	talkStart   time.Time
	quietStreak int
}

func newSpeechGate(dec pcmDecoder) *speechGate {
	return &speechGate{
		dec:     dec,
		heardCh: make(chan struct{}),
	}
}

// activate marks the moment the call was answered; audio fed before this is
// ignored. Idempotent.
func (g *speechGate) activate(now time.Time) {
	_ = now
	g.mu.Lock()
	defer g.mu.Unlock()
	g.activated = true
}

// heard is closed exactly once when speech has been detected per the rule.
// It is never closed otherwise.
func (g *speechGate) heard() <-chan struct{} {
	return g.heardCh
}

// feedOpus decodes one packet (decoder errors are ignored/skipped) and calls
// feedPCM with the decoded samples.
func (g *speechGate) feedOpus(packet []byte, now time.Time) {
	g.mu.Lock()
	if g.fired {
		g.mu.Unlock()
		return
	}
	dec := g.dec
	g.mu.Unlock()

	if dec == nil {
		return
	}

	// 16 kHz Opus packets are at most 120 ms of audio, but room for more is
	// harmless.
	const maxOpusFrame = 120 * gateRate / 1000
	pcm := make([]int16, maxOpusFrame)
	n, err := dec.Decode(packet, pcm)
	if err != nil || n <= 0 {
		return
	}
	g.feedPCM(pcm[:n], now)
}

// feedPCM consumes 16 kHz mono samples of any length, buffering partial
// frames internally. It classifies each complete 20 ms frame and fires once
// when the rule below is met. now is the arrival time of the LAST sample in
// pcm.
func (g *speechGate) feedPCM(pcm []int16, now time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()

	if !g.activated || g.fired {
		return
	}

	g.buf = append(g.buf, pcm...)
	sampleDur := time.Second / gateRate

	for len(g.buf) >= gateFrame {
		frame := g.buf[:gateFrame]
		trailing := len(g.buf) - gateFrame
		frameTime := now.Add(-time.Duration(trailing) * sampleDur)

		voiced := isSpeechFrame(frame)
		g.updateFrameLocked(voiced, frameTime)

		g.buf = g.buf[gateFrame:]
		if g.fired {
			g.buf = nil
			return
		}
	}
}

func (g *speechGate) updateFrameLocked(voiced bool, frameTime time.Time) {
	old := g.hist[g.histIdx]
	if old {
		g.voicedCount--
	}
	if voiced {
		g.voicedCount++
	}
	g.hist[g.histIdx] = voiced
	g.histIdx = (g.histIdx + 1) % len(g.hist)

	if !g.talkStarted && g.voicedCount >= talkStartFrames {
		g.talkStarted = true
		g.talkStart = frameTime
		g.quietStreak = 0
	}

	if g.talkStarted {
		if voiced {
			g.quietStreak = 0
		} else {
			g.quietStreak++
		}

		if g.quietStreak >= quietFrames || frameTime.Sub(g.talkStart) >= time.Duration(gateMaxWaitMS)*time.Millisecond {
			if !g.fired {
				g.fired = true
				close(g.heardCh)
			}
		}
	}
}

// isSpeechFrame classifies one 320-sample frame: loud enough AND not tonal.
func isSpeechFrame(frame []int16) bool {
	if len(frame) != gateFrame {
		return false
	}

	var sumSq float64
	for _, v := range frame {
		f := float64(v)
		sumSq += f * f
	}
	rms := math.Sqrt(sumSq / gateFrame)
	if rms < 300 {
		return false
	}

	const fftN = 512
	a := make([]complex128, fftN)
	for i, v := range frame {
		x := float64(v)
		w := 0.5 * (1 - math.Cos(2*math.Pi*float64(i)/(gateFrame-1)))
		a[i] = complex(x*w, 0)
	}
	fftRadix2(a)

	power := make([]float64, fftN)
	for k := 0; k < fftN; k++ {
		re := real(a[k])
		im := imag(a[k])
		power[k] = re*re + im*im
	}

	const minBin = 4   // ~100 Hz at 16 kHz / 512
	const maxBin = 112 // ~3500 Hz

	bandEnergy := 0.0
	sumSafe := 0.0
	logSum := 0.0
	count := 0
	for k := minBin; k <= maxBin; k++ {
		p := power[k]
		bandEnergy += p
		safe := p + 1e-9
		sumSafe += safe
		logSum += math.Log(safe)
		count++
	}

	flatness := math.Exp(logSum/float64(count)) / (sumSafe / float64(count))
	if flatness < 0.01 {
		return false
	}

	type peak struct {
		center int
		energy float64
	}
	var peaks []peak
	for k := minBin + 1; k < maxBin; k++ {
		if power[k] >= power[k-1] && power[k] >= power[k+1] {
			energy := power[k-1] + power[k] + power[k+1]
			peaks = append(peaks, peak{center: k, energy: energy})
		}
	}

	if len(peaks) == 0 {
		return true
	}

	sort.Slice(peaks, func(i, j int) bool {
		return peaks[i].energy > peaks[j].energy
	})

	var selected []peak
	for _, p := range peaks {
		overlap := false
		for _, s := range selected {
			if absInt(p.center-s.center) <= 2 {
				overlap = true
				break
			}
		}
		if !overlap {
			selected = append(selected, p)
			if len(selected) == 3 {
				break
			}
		}
	}

	peakEnergy := 0.0
	for _, p := range selected {
		peakEnergy += p.energy
	}
	if bandEnergy > 0 && peakEnergy/bandEnergy > 0.8 {
		return false
	}

	return true
}

func fftRadix2(a []complex128) {
	n := len(a)

	// bit-reversal permutation
	for i, j := 0, 0; i < n; i++ {
		if i < j {
			a[i], a[j] = a[j], a[i]
		}
		m := n >> 1
		for j >= m && m > 0 {
			j -= m
			m >>= 1
		}
		j += m
	}

	for length := 2; length <= n; length <<= 1 {
		ang := -2 * math.Pi / float64(length)
		wlen := complex(math.Cos(ang), math.Sin(ang))
		for i := 0; i < n; i += length {
			w := complex(1, 0)
			half := length >> 1
			for j := 0; j < half; j++ {
				u := a[i+j]
				v := a[i+j+half] * w
				a[i+j] = u + v
				a[i+j+half] = u - v
				w *= wlen
			}
		}
	}
}

func absInt(x int) int {
	if x < 0 {
		return -x
	}
	return x
}
