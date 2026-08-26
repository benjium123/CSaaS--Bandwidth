# Agent workers (P7+)

Separate package, separate venv — `pip install -r agents/requirements.txt` (Python >= 3.10).
The backend never imports from here; the SFU room is the only interface.

- `audio_metrics.py` — pure measurement core (no livekit): paced-playback model that
  reports **queue depth separately from rt ratio** (rt=1.0 alone proves nothing — D5) and
  the dropped-sentence-**tail** detector (`tail_energy_ratio`). Unit-tested anywhere.
- `echo_agent.py` — `python -m agents.echo_agent dev` — joins `call-*` rooms and echoes
  the SIP participant's audio back. `ECHO_MODE=barge` enables the barge-in variant which
  measures speech-onset → playback-stop latency. Its end-of-call log line carries the P7
  gate numbers (rt, underruns, max/avg depth).
- `replay_harness.py` — `python -m agents.replay_harness --url ws://127.0.0.1:7880
  --api-key csaas-media --api-secret ... --room call-replay-1 --wav caller.wav
  --report out.json --expect-echo` — plays recorded caller audio into a room at real-time
  pacing, records what comes back, and gates on: rt ≥ 0.97, zero underruns,
  tail_energy_ratio ≥ 0.5, every utterance returned. **This is the conversation-replay
  gate: every later audio change must pass it before deploy.**

Runtime verification needs a live LiveKit (deploy/livekit/); only `audio_metrics` is
testable offline — that is by design, the rest IS the integration.
