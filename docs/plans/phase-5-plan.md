# Phase 5 — Voice core

## Goal
Outbound call creation, inbound answer, recording, blind transfer, DTMF gather, AMD, and a
call log — on the same multi-carrier fabric P3b built for messaging. The load-bearing idea
is D2 applied to voice: the internal interface is **event in → command out, async** (the
Telnyx shape); the Bandwidth adapter serializes the emitted commands down into one BXML
document per webhook response. A "call" is N **legs** (D6/R7): callbacks fire per leg, and
the transfer/AMD cases are exactly where a single-row call model corrupts.

## Non-goals (later phases)
- Media streaming / AI agent on the call (P7–P8, pending LiveKit decision)
- Browser softphone (P6)
- Conferencing / barge (P12)
- Outbound dialer campaigns (P11)

## Design (settled here, not by the implementer)

### State machines — monotonic by rank, same shape as messages/registration
**Call**: `queued(0) → initiated(10) → ringing(20) → answered(30) → bridged(35) →
completed(40) | failed(40) | busy(40) | no_answer(40) | canceled(40)`.
All rank-40 statuses are terminal; one terminal never replaces another.
**CallLeg**: `created(0) → dialing(10) → ringing(20) → answered(30) → hungup(40)`
plus `failed(40)`. A transfer creates a NEW leg; it never mutates the old one.
Call status is **derived**: answered when any leg answers; terminal only when every leg is
terminal (the surviving leg of a transfer keeps the call alive).

### Voice command protocol (`app/providers/voice.py`)
Frozen dataclasses, carrier-neutral:
`Speak(text, voice)`, `Play(url)`, `Gather(max_digits, terminating_digit, timeout_seconds,
prompt: Speak|Play|None, action_tag)`, `StartRecording(channels)`, `StopRecording`,
`Transfer(to, from_)` (blind), `Hangup()`, `Pause(seconds)`.
`VoiceCarrier` protocol: `create_call(to, from_, *, amd, tag) -> CallResult`,
`verify_voice_webhook`, `parse_voice_webhook(raw) -> list[VoiceEvent]`,
`render_commands(commands) -> str|None` (BXML doc for Bandwidth; None for Telnyx which
issues API actions instead via `execute_commands(call_ref, commands)`).
`VoiceEvent`: `event_type` canonical ∈ {call_initiated, call_ringing, call_answered,
call_bridged, call_hungup, machine_detected, human_detected, dtmf_received,
recording_ready, transfer_completed}, `provider_call_id`, `provider_leg_id`, `to`, `from_`,
`digits`, `recording_url`, `duration_seconds`, `tag`, `provider_event_id`, `occurred_at`.

### Webhook contract (D6)
- Dedupe on `(carrier, provider_event_id)` persisted in `voice_events` — DB constraint,
  not an application check.
- Handlers apply state transitions only; 2xx fast. For Bandwidth the HTTP response body IS
  the rendered BXML of whatever commands the service emitted for that event.
- An unmatched event (unknown provider_call_id) is stored and 200'd, never 404'd —
  Bandwidth retries any non-2xx for 24h.

### Recordings
`recording_ready` → download via carrier-authenticated fetch (reuse the media pipeline's
allowlist discipline: only the carrier's own host gets credentials) → store via
`app/storage` → serve from our origin with auth. Carrier URL is NEVER handed to the UI.

### AMD
Outbound calls accept `machine_detection: "off"|"async"`. Async AMD →
`machine_detected`/`human_detected` events recorded on the leg (`amd_result` column).
This phase only records the verdict and exposes it; acting on it is P11's business.

## Allowed files (implementer)
- app/providers/voice.py (protocol + commands — Fable authors, implementer reads only)
- app/providers/bandwidth/voice.py, app/providers/bandwidth/voice_webhooks.py
- app/providers/telnyx/voice.py
- app/services/calls.py
- app/api/routes/calls.py
- app/api/routes/webhooks.py (additive: voice routes only)
- app/main.py (router include only)
- tests/test_voice_*.py
- frontend: src/pages/Calls.tsx + route/nav wiring + api types regen

## Forbidden
- app/models/** and migrations/** (Fable authors the schema)
- app/providers/domain.py, registry.py, health.py, base.py (messaging fabric is frozen)
- app/services/messaging.py, sender.py; app/routing/**
- .env*, app/config.py (voice settings already exist)

## Test spec
Unit:
- [ ] call rank never decreases; one terminal never replaces another (per leg AND per call)
- [ ] transfer creates a second leg; hangup of leg 1 does NOT complete the call while leg 2
      is live; call completes only when the last leg ends
- [ ] duplicate `provider_event_id` applies once (DB-constraint dedupe, injected duplicate)
- [ ] out-of-order: `call_hungup` before `call_answered` → answered ignored afterwards
- [ ] BXML golden tests: each command renders exact expected XML; command list renders in
      order inside one `<Response>`; XML special chars in Speak text are escaped
- [ ] Telnyx executes commands as API actions (mock transport asserts endpoint + body)
- [ ] AMD `machine_detected` recorded on the right leg
- [ ] unknown provider_call_id event → stored + 200, not 404
- [ ] recording fetch: credentials only to carrier host; stored via app/storage; API serves
      it with auth; carrier URL absent from every API response
Integration:
- [ ] POST /api/v1/calls creates call+leg, dispatches via carrier (fake), returns 201
- [ ] full inbound lifecycle via webhook sequence: initiated→ringing→answered→hungup
      walks the state machine; duplicates replayed mid-sequence change nothing
- [ ] blind transfer end-to-end: transfer command → new leg → old leg hungup → call still
      answered → second leg hungup → call completed with both legs in history
- [ ] tenancy: org B cannot see org A's calls
Pass criteria: ALL green, ruff clean, frontend typecheck+vitest green.

## Deploy
yes (after CI green)
