# Phase 10 — AI SMS agent

**WS:** 5 · **Deploy:** yes · **Planned:** 2026-08-28 (Fable)

## Goal

An org can flip a switch on its agent profile and the AI answers inbound SMS: it holds a
multi-turn conversation with per-thread memory, calls the SAME tools the voice agent has
(contact context, appointment booking, KB search), and hands off to a human on a keyword,
a turn ceiling, an explicit tool call, or any failure. Every AI-originated send goes
through `send_message` and therefore through the full compliance gate — there is no
second send path.

## Design rulings (settled — do not relitigate in implementation)

- **DR-1: The SMS agent runs IN the backend, not in the LiveKit worker.** There is no
  realtime audio; a turn is request→response. The voice worker exists out-of-process for
  latency reasons that do not apply here. The "shared brain" is the SERVICE layer: the SMS
  surface calls `agent.get_contact_context`, `agent.book_appointment`, `kb.search_chunks`
  in-process — never over the worker HTTP seams (an in-process HTTP hop adds failure
  modes for nothing).
- **DR-2: Trigger is post-commit, idempotent, and never blocks the webhook.** After
  `_ingest_inbound` commits, spawn an asyncio task with its OWN session. Idempotency is a
  ROW, not a hope: `agent_sms_turns.inbound_message_id` is UNIQUE — a webhook redelivery
  finds the row and does nothing. LLM latency must never push the carrier webhook toward
  its timeout.
- **DR-3: One inbound → at most one outbound.** The agent never initiates (that is P11),
  never double-replies, and never replies to a message the compliance keyword engine
  already answered (STOP/HELP have auto-replies; an AI reply on top is a compliance
  violation and reads deranged).
- **DR-4: The gate is not optional and not special-cased.** AI sends call
  `services/messaging.send_message` with no exemption. If the gate blocks or defers, the
  turn is recorded `blocked` and the agent does NOT retry — a held send releasing later
  via the sweeper is fine; a retry loop against an opt-out is not.
- **DR-5: Handoff is a state machine on the thread.** `message_threads.ai_state`:
  `off | active | handed_off`. Transitions: enable → `active`; keyword / ceiling /
  `handoff_to_human` tool / LLM error → `handed_off` (+ event on the bus, same shape as
  voice `publish_handoff`); a HUMAN sending a manual reply in an `active` thread →
  `handed_off` (takeover is implicit — an operator typing must silence the bot without a
  second click). Re-arming is an explicit API call. `off` is the default forever until an
  operator enables SMS on the profile.
- **DR-6: LLM access is a thin httpx client, no new dependencies.** Anthropic
  `/v1/messages` and OpenAI `/v1/chat/completions`, tool calling included, keyed from the
  existing `anthropic_api_key` / `openai_api_key` settings, provider/model chosen by the
  same AgentProfile fields the voice agent uses. No SDKs — httpx is already a runtime dep
  and the surface we use is tiny.
- **DR-7: Turn ceiling counts AI replies in the thread since last (re)arm**, not messages.
  Default 10, per-profile. Reply length is clamped (default 480 chars ≈ 3 segments).

## Schema (Tier-1, already done by Fable — implementers do not touch)

- `consent_events.seq` — monotonic tiebreaker; `latest_consent` orders by
  `(created_at, seq, id)`. Fixes the recorded coin-flip bug.
- `agent_profiles.sms_enabled` (bool, default false), `sms_turn_ceiling` (int, default 10),
  `sms_handoff_keywords` (JSON list, default `["human","agent","representative","person","stop the bot"]`),
  `sms_max_reply_chars` (int, default 480).
- `message_threads.ai_state` (`off|active|handed_off`, default `off`).
- `agent_sms_turns`: id, org_id, thread_id, inbound_message_id (UNIQUE),
  outbound_message_id (nullable), status (`replied|skipped|blocked|handoff|error`),
  detail, timestamps.
- Migration `0011_consent_seq_sms_agent` (additive).

## Allowed files (implementer may read anything; WRITE only these)

Backend implementer:
- `backend/app/services/llm_client.py` (new — Flash draft provided, adapt as needed)
- `backend/app/services/sms_agent.py` (new — turn engine)
- `backend/app/services/messaging.py` (ONLY: post-commit trigger hook at the end of
  `_ingest_inbound`, and the human-takeover flip in `send_message`; no other edits)
- `backend/app/api/routes/agent.py` (profile fields + thread ai_state endpoints)
- `backend/app/api/routes/inbox.py` (expose ai_state; re-arm endpoint may live here)
- `backend/tests/test_sms_agent.py` (new)
- `backend/tests/test_agent_profiles.py` (extend for new fields)

Frontend implementer:
- `frontend/src/api/hooks.ts`, `frontend/src/api/types.gen.ts` (regenerated)
- `frontend/src/pages/InboxPage.tsx` (AI chip + Take over / Re-arm button)
- `frontend/src/pages/AgentPage.tsx` (SMS section on profile form)
- matching test files under `frontend/src/pages/__tests__/`

## Forbidden (all implementers)

- `backend/app/models/**`, `backend/migrations/**` — schema is done, hands off
- `backend/app/compliance/**` — the gate is evidence, not clay
- `backend/app/providers/**`, `backend/app/routing/**`
- `agents/**` (voice worker untouched in P10)
- `.env*`, `deploy/**`, CI config, `pyproject.toml` (no new dependencies)
- the frozen seam tests (`test_compliance_seam.py` and P1/P2 spies)

## Test spec

Unit / integration (backend):
- [ ] inbound on sms_enabled profile + `active` thread → exactly one outbound, through the
      gate (spy proves `check_outbound` ran), `agent_sms_turns` row `replied`
- [ ] webhook redelivery of same inbound → no second reply (unique row, turn `skipped`/noop)
- [ ] opted-out contact → gate blocks → turn `blocked`, no send, no retry
- [ ] STOP inbound → keyword engine's auto-reply only; agent turn `skipped`
- [ ] handoff keyword ("human") → no AI reply, thread `handed_off`, bus event published
- [ ] turn ceiling: 10th AI reply flips to `handed_off` with a final handoff message
- [ ] `handoff_to_human` tool call → same as keyword path
- [ ] LLM 5xx/timeout → turn `error`, thread `handed_off`, webhook outcome unaffected
- [ ] human manual send in `active` thread → thread flips `handed_off`, bot stays silent on
      next inbound
- [ ] re-arm endpoint → `active` again, bot answers next inbound
- [ ] tool call round-trip: model asks `book_appointment` → appointment row exists →
      final text reply mentions it (fake LLM transport; NO live API calls in tests)
- [ ] reply clamped to `sms_max_reply_chars`
- [ ] `sms_enabled=false` (default) → nothing happens anywhere

Frontend:
- [ ] inbox thread shows AI state chip; Take over calls the endpoint; Re-arm shown when
      `handed_off`
- [ ] profile form round-trips the four SMS fields

Manual (the phase gate, run against the live deploy):
- [ ] a 10-turn conversation from a real handset, one tool call, handoff on "human",
      compliance footer/gate intact

Pass criteria: full backend suite green (SQLite 3.10+3.12 AND Postgres), frontend suite
green, OpenAPI drift gate green. Live LLM calls only via env keys at runtime — never in CI.

## Deploy

yes — after review: migrate + restart on the VPS, then run the manual gate.
