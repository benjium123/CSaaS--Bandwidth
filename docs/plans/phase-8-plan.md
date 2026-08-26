# Phase 8 — AI voice agent v1 (LiveKit Agents)

D14/D17 replace the original Pipecat-on-`<StartStream>` plan: the agent is a **LiveKit
Agents worker** in the same rooms the softphone uses. P7's echo agent proved the loop;
P8 swaps the echo for a real pipeline.

## Pipeline (v1)
STT **Deepgram Nova-3** (streaming) → LLM (config-driven: `LLM_PROVIDER` anthropic |
openai | deepseek; default anthropic claude-haiku — cheap + fast for voice) → TTS
**ElevenLabs Flash v2.5**. VAD/turn detection: livekit-agents' silero VAD + its turn
detector — **endpointing is the biggest latency lever; its knobs must be env-tunable
without code changes** (`AI_ENDPOINT_MIN_SILENCE`, `AI_ALLOW_INTERRUPTIONS`, etc.).
Barge-in: `allow_interruptions=True` — the framework clears queued TTS on caller speech;
the replay + barge gates from P7 apply to this agent unchanged.

## The agent ↔ backend contract (the part that is OURS)
The worker is a separate process with **no DB access** (agents/README law). Two
authenticated HTTP seams, both JWT-signed with the LiveKit secret (reuse
`mint_access_token`/`verify` shapes — no new auth scheme):
1. `GET /api/v1/agent/context/{call_id}` — the worker resolves its job (dispatch
   `metadata` = call id) into: org name, contact number, direction, system prompt,
   greeting, allowed handoff numbers. Org-level agent config lives in a new
   `agent_profiles` table (org_id, name, system_prompt, greeting, voice_id, llm fields,
   is_default) — managed via `/api/v1/agent/profiles` CRUD (compliance untouched).
2. `POST /api/v1/agent/transcript` — batched segments {call_id, role user|agent, text,
   at_ms}; stored in `call_transcripts` (migration 0008); served inside the call detail
   and rendered on the Calls page. Transcripts are the P9 raw material — capture from v1.

## Deliverables
- `agents/ai_agent.py` (+ requirements extras) — AgentSession pipeline, per-call context
  fetch, transcript forwarding, hangup on caller goodbye/silence timeout, metrics log
  line per call (ttfb voice-to-voice p50/p95 from livekit-agents metrics API).
- Backend: migration 0008 (`agent_profiles`, `call_transcripts`); agent auth dependency
  (JWT identity "agent-worker", signed with livekit secret); the two seams; profile CRUD;
  transcript in call detail API; auto-dispatch — org toggle `agent_profiles.is_default`
  + `answer_mode="ai"` on OrgNumber? NO: v1 keeps dispatch EXPLICIT (console button /
  API); auto-answer policy is P9 scope. Do not build policy before the agent works.
- Console: transcript panel in call detail; agent profile editor (name, prompt, greeting,
  voice); "Send AI agent" button on a live room call (uses P7's dispatch endpoint with
  agent_name="ai").
- Tests: seams (auth required, wrong-signature 401, org scoping), transcript
  batching/ordering/idempotency (dedupe on (call_id, at_ms, role) unique), profile CRUD
  + RBAC, context resolution incl. default profile fallback; agents-side: pure-logic
  units only (prompt assembly, batching buffer) — the pipeline itself is the VPS gate.

## Runtime gate (VPS, deferred like P7 — published in docs/research/)
p50 ≤ 700 ms / p95 ≤ 1100 ms voice-to-voice measured by the framework's metrics;
conversation-replay green; barge-in interrupts without deleting tails; STT bake-off
(Nova-3 vs AssemblyAI on recorded caller audio) — R3.

## Forbidden to implementers
app/models/**, migrations/** (Fable authors 0008), app/config.py, app/providers/**,
app/voice_plane/livekit_api.py, deploy/**.
