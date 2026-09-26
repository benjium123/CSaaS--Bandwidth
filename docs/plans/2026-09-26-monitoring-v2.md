# Call monitoring v2 — record, transcribe after, keep only proof

Status: P1 BUILT 2026-09-27 (recorder + wiring, not deployed yet). Replaces the P43 live listener (agents/call_monitor.py).

## Goal
Every monitored call is recorded by lkrec, our own small Go recorder (recorder/), transcribed on our own server with a
light Whisper model, and reviewed by the existing DeepSeek fraud check. How fast a call is reviewed
depends on how risky the account is. Clean recordings are deleted; scam/suspicious ones are kept as
evidence. No per-minute transcription fees, no always-on listener.

## 1. Monitoring rule (who is recorded, how fast it is reviewed)

| Account state | Calls recorded | Review timing |
|---|---|---|
| Paused / restricted / watch (flagged) | 100% | Right after each call (queue, ~2-5 min) |
| New account, days 0-7 | 100% | Right after each call |
| New account, days 8-30 | 50% | Nightly |
| New account, days 31-60 | 20% | Nightly |
| Established (60+ days, normal) | 5% (never 0) | Nightly |

Escalation (automatic, detection only — enforcement stays operator-approved, monitor_auto_action=False):
- "suspicious" verdict -> account goes to watch -> 100% + right-after-call review.
- "scam" verdict -> watch + immediate ops alert + restriction RECOMMENDATION (operator applies).
- Behaviour signals that need no audio (short calls, low answer rate, STOP spikes, spam flags,
  volume spikes) keep feeding the same risk score, as today.
De-escalation: watch drops back to its age tier after 14 days with no suspicious/scam verdict.

All percentages/days are settings (config.py), not code.

## 2. Nightly window and morning report
- TCPA calling ends 9pm in the callee's time zone; for the 48 states the last window closes at
  9pm Pacific = 11pm Central. Nightly batch runs 00:30-07:00 America/Chicago, low CPU priority.
- 07:00 Central report (Ops > Monitoring + email to ops): calls reviewed, verdict counts,
  every suspicious/scam call with quotes and a play link, accounts escalated/de-escalated,
  backlog left (if the night did not finish).

## 3. Recording: lkrec (our own Go recorder, no per-minute cost)
Egress was installed and measured 2026-09-26 at ~0.65 CPU core per recorded call because it decodes and re-encodes the audio, so it was rejected and uninstalled. lkrec joins the room as an agent-kind participant and copies each side's Opus packets unchanged into one .ogg per side on a shared timeline (gaps from mute/DTX/late start filled with Opus silence frames, so the two files line up). Measured on the server with 5 simulated 2-party calls: 4 MB RAM idle, ~26-35 MB with 5 calls, ~5-7% of one core per recorded call (Egress ~65%). Files: ~0.23 MB per minute per side. Crash-safe: a manifest is written atomically when the recording ends; after a crash, orphaned files get a 'recovered' manifest (tested: killed mid-call, 20 s recovered and playable); the backend re-starts the rooms of calls still in progress (resume_tick). Backend: monitor_calls.on_livekit_event starts lkrec for monitored LiveKit calls; falls back to the old live listener if the recorder or the announcement is unavailable. Sweeper ingest_tick moves finished files into the object store (org/<org>/monitor/<call>/...) recorded in call.extra['monitor_recordings'] — monitoring audio is NOT a CallRecording row, so customers never see it.

### Compression with no quality loss
- The call audio inside LiveKit is already Opus. lkrec writes that Opus stream into an .ogg
  file WITHOUT re-encoding -> bit-for-bit what the call carried, zero added loss.
- Keep the two sides as two files (agent.ogg, customer.ogg): gives speaker labels for free in the
  transcript; an ops player (P3) can play them together, since they share one timeline.
- Size: ~0.23 MB per minute per side (vs ~3.8 MB/min for today's WAV layout).

### Retention
| Verdict | Monitoring-only recording | Customer's own recording (plan has recording) |
|---|---|---|
| ok | deleted 7 days after review | kept per plan retention |
| suspicious | 180 days, ops-only | same, plus the hold |
| scam | evidence hold 2 years, ops-only, customer cannot delete | same |

implemented now = ok/skipped deleted after monitor_recording_keep_days (7); suspicious/scam kept (180 d / 2 y holds are P2).

## 4. Transcription: faster-whisper on our server (free)
- Until P2 lands, P1 transcribes each side with Deepgram (already paid for); per-side files give speaker labels without diarization.
- New small "transcriber" container (or sweeper job) with a CPU cap and low priority; loads the
  model only when there is work, so ~0 MB when idle.
- Model: faster-whisper base.en int8 (light) for all calls; any call the AI marks suspicious/scam
  is re-transcribed with small.en for a better evidence transcript.
- Existing DeepSeek review (monitor_calls.review_tick) reads the transcript unchanged.

## 5. Announcement
DECIDED — played only on recorded calls (user decision 2026-09-26). lkrec plays the org's announcement (ElevenLabs TTS rendered once per text as Ogg/Opus and cached in the shared dir) into the call as soon as the phone side answers, and writes nothing before it has finished; if it cannot be played, the call is not recorded.

## 6. Retire
- The live listener (agents/call_monitor.py, csaas-call-monitor, 450 MB, Deepgram live) once v2
  has run clean for a week.
- The idle AI agent worker (1.27 GB) until AI agents are configured.

## Phases (each testable, each deployable)
- P1 lkrec recorder + backend wiring (built 2026-09-27; deploy pending a real test call). Deploy: yes.
- P2 Transcriber + policy: switch transcription from Deepgram to faster-whisper; faster-whisper job, priority vs nightly queue, the tier rule above, escalation/de-escalation, evidence hold. Deploy: yes.
- P3 Report + retire: 07:00 report (Ops page + email), switch off live listener and AI worker. Deploy: yes.

## New dependencies (need approval)
- Go build of recorder/ (livekit/server-sdk-go v2.18.1, pion/webrtc v4) — approved 2026-09-26.
- Python package faster-whisper (+ ctranslate2) in the transcriber.
