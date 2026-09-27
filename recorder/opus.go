package main

import "gopkg.in/hraban/opus.v2"

// opusPCM decodes the callee's Opus packets to 16 kHz mono for the speech gate
// (libopus via cgo; built with -tags nolibopusfile).
type opusPCM struct{ d *opus.Decoder }

func newOpusDecoder() (pcmDecoder, error) {
	d, err := opus.NewDecoder(gateRate, 1)
	if err != nil {
		return nil, err
	}
	return &opusPCM{d: d}, nil
}

func (o *opusPCM) Decode(packet []byte, pcm []int16) (int, error) {
	return o.d.Decode(packet, pcm)
}
