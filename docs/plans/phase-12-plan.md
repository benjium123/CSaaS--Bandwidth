# Phase 12 — IVR, queues, voicemail, routing

**WS:** 3, 7 · **Deploy:** yes · **Planned:** 2026-08-29 (Fable)

## Goal

An inbound call stops hitting the P6 "not configured" stub: it routes through a per-org
CALL FLOW — a versioned graph of IVR menus (DTMF), business-hours/holiday branches, ring
groups (simultaneous/sequential), queues with hold audio and callback capture, and
voicemail with a transcription seam. Supervisors can monitor, whisper to the agent, or
barge into a live room call. The live gate (2-level IVR → queue → hold music → agent →
whisper) needs B2; everything below is built and proven against fakes locally.

## Design rulings (settled — do not relitigate in implementation)

- **DR-1: One flow engine, two executors.** The flow is a JSON graph interpreted by a
  PURE engine (`services/flow_engine.py`): `(flow, node_id, event) -> (actions, next
  node)`. No DB, no IO — fully unit-testable. Executors translate actions:
  the **carrier executor** (P5 command path) maps to Speak/Play/Gather/Transfer/Hangup
  BXML — it REPLACES `DEFAULT_INBOUND_COMMANDS` in the voice webhook exactly as the P6
  comment promised; the **room executor** (LiveKit path) implements ring/queue/answer
  natively (targeted `call.ring`, queue entries, claim-to-answer).
- **DR-2: Node types v1:** `menu` (prompt + DTMF options + timeout/invalid retries →
  default branch), `hours` (business-hours table branch: open/closed/holiday),
  `ring_group`, `queue`, `voicemail`, `speak` (say + continue), `hangup`. Speech-intent
  menus are OUT of v1 (they need the agent worker; recorded in OPEN_ISSUES, the `menu`
  node schema reserves a `speech_keywords` field so flows won't break when it lands).
- **DR-3: Flows are VERSIONED and bind to numbers.** `call_flows.definition` is
  immutable per version (editing creates a new version; active version pinned on the
  flow). Binding: `org_numbers.call_flow_id` (nullable — NULL keeps today's behavior).
  A mid-call flow edit never changes a call in flight: the call pins the version it
  started with (`calls.extra["flow_version"]`).
- **DR-4: Flow validation is a pre-save gate.** A flow that references a missing
  node/ring-group/queue, has an unreachable node, or has a menu option pointing nowhere
  is REJECTED at save (422 with the exact node ids). Runtime never discovers a broken
  graph. On any runtime engine error the call falls back to voicemail if a voicemail
  node exists anywhere in the flow, else polite hangup — never dead air.
- **DR-5: Ring groups.** `ring_groups` (member user ids, strategy
  `simultaneous|sequential`, per-step `ring_timeout_seconds`). Room path: targeted
  `call.ring` events carry `ring_user_ids` — consoles ignore rings not addressed to
  them (an empty list keeps today's broadcast). Sequential steps advance on a sweeper
  tick when the offer times out. Claim stays first-answer-wins via the existing
  `call.handoff.claimed` event; a claim cancels outstanding offers.
- **DR-6: Queues.** `call_queues` (hold audio URL nullable, `max_wait_seconds`,
  overflow → `voicemail|hangup|callback`), `queue_entries` state machine:
  `waiting → offered → connected` | `abandoned` (caller hung up) | `overflowed` |
  `callback_requested`. Position is DERIVED (count of earlier `waiting` entries), never
  stored. Agents pull via "next in queue" (offer goes only to the pulling agent) or the
  sweeper offers the head to the queue's ring group. Callback queuing v1 = capture:
  the entry flips `callback_requested` (caller pressed the callback digit), an event
  publishes, and the entry lands in a queryable list — auto-dialing the callback rides
  P11's dialer (a manual "dial now" button in v1, wired to the P11 machinery).
- **DR-7: Hold music.** Carrier path: `Play(url)` loop while waiting (the P5 command
  exists; Bandwidth renders it). Room path v1: silence + periodic comfort `Speak` via
  the carrier leg is NOT possible without an audio-publishing participant — room-path
  hold audio needs the agents worker and is recorded in OPEN_ISSUES (I5), NOT faked.
  The gate's hold-music leg runs on the carrier executor.
- **DR-8: Voicemail.** Carrier path: Speak greeting → StartRecording (P5 F3 machinery)
  → `voicemails` row on `recording_ready` (links the CallRecording). Room path: same
  OPEN_ISSUES note as DR-7 (needs worker; P9 voicemail-drop is OUTBOUND and does not
  help here). Transcription is a SEAM: `voicemails.transcript_status`
  `pending|done|failed|disabled`; the sweeper transcribes pending voicemails via
  Deepgram REST (`DEEPGRAM_API_KEY`) when configured, else marks `disabled` — honest
  about B4, no fake transcripts.
- **DR-9: Supervisor ops are room/token operations, not carrier features** (D17's whole
  point; R7's Bandwidth conference caps never apply because we never use carrier
  conferences). `monitor`: subscribe-only token (`canPublish=false`). `whisper`:
  publish token + server-side `update_subscriptions` forcing the SIP (caller)
  participant to NOT subscribe to the supervisor's track — the deny list is enforced
  server-side, never by client politeness. `barge`: full token. Every supervisor action
  writes a VoiceEvent (`supervisor.monitor|whisper|barge`) — auditable. RBAC: a new
  `calls:supervise` permission, granted to admin/owner roles only.
- **DR-10: Business hours are a voice concern, not ComplianceSettings.**
  `business_hours` table: org-scoped, IANA timezone, per-weekday `[open, close]` list
  (multiple windows allowed), `holidays` (ISO dates). The `hours` node references one.
  SMS quiet hours stay untouched in compliance — different law, different table.
- **DR-11: LiveKit API additions are Fable-owned.** `livekit_api.py` gains
  `update_subscriptions(room, identity, track_sids, subscribe)` and
  `mint_access_token` gains `can_publish`/`can_subscribe` kwargs. `voice_plane/` stays
  forbidden to implementers; Fable makes these two edits with the schema.

## Schema (Tier-1, done by Fable — implementers do not touch)

Migration `0013_ivr_queues` (additive):
- `call_flows`: id, org_id, name, version (int, default 1), status (`draft|active|archived`),
  definition (PortableJSON), entry_node (str), created_by, timestamps.
  UNIQUE (org_id, name, version).
- `org_numbers.call_flow_id` (nullable FK → call_flows.id, SET NULL).
- `ring_groups`: id, org_id, name, strategy (`simultaneous|sequential`), member_user_ids
  (PortableJSON list), ring_timeout_seconds (default 20), timestamps.
- `call_queues`: id, org_id, name, hold_audio_url (nullable), max_wait_seconds
  (default 300), overflow (`voicemail|hangup|callback`), ring_group_id (nullable FK),
  timestamps.
- `queue_entries`: id, org_id, queue_id (FK), call_id (FK), state
  (`waiting|offered|connected|abandoned|overflowed|callback_requested`), offered_user_id
  (nullable), callback_e164 (nullable), enqueued_at, resolved_at (nullable), timestamps.
  Index (queue_id, state).
- `business_hours`: id, org_id, name, timezone (str), schedule (PortableJSON:
  {"mon": [["09:00","17:00"]], ...}), holidays (PortableJSON list of ISO dates),
  timestamps.
- `voicemails`: id, org_id, call_id (FK), recording_id (nullable FK), greeting_node
  (nullable str), transcript (nullable Text), transcript_status
  (`pending|done|failed|disabled`), status (`new|read`), timestamps.

## Allowed files (implementer may read anything; WRITE only these)

Backend:
- `backend/app/services/flow_engine.py` (exists after Fable installs the Flash draft —
  extend only if a ruling demands)
- `backend/app/services/flows.py` (new — flow CRUD + validation gate + version pinning)
- `backend/app/services/routing_exec.py` (new — carrier + room executors, ring-group
  stepping, queue offers; sweeper tick `routing_tick`)
- `backend/app/services/voicemail.py` (new — voicemail rows, transcription seam)
- `backend/app/services/supervisor.py` (new — monitor/whisper/barge token flows per
  DR-9, VoiceEvent audit rows)
- `backend/app/services/sweeper.py` (ONLY add `routing_tick` + voicemail transcription
  to run_once)
- `backend/app/api/routes/webhooks.py` (ONLY replace the DEFAULT_INBOUND_COMMANDS
  branch with the flow lookup → carrier executor; the P6 comment marks the spot)
- `backend/app/api/routes/flows.py` (new: flows/ring-groups/queues/hours/voicemails
  CRUD + supervisor endpoints) + one include line in `backend/app/main.py`
- `backend/tests/test_flow_engine.py`, `test_flows.py`, `test_routing_exec.py`,
  `test_voicemail.py`, `test_supervisor.py`

Frontend:
- `frontend/src/pages/FlowsPage.tsx` (flow builder v1: node list editor, not a canvas),
  `frontend/src/pages/QueuesPage.tsx` (queues + voicemails inbox + callback list),
  nav/router wiring, regenerated `hooks.ts`/`types.gen.ts`, matching tests

## Forbidden (all implementers)

- `backend/app/models/**`, `backend/migrations/**`
- `backend/app/voice_plane/**` (Fable makes the DR-11 edits), `backend/app/services/calls.py`
- `backend/app/services/messaging.py`, `compliance/**`, `providers/**`, `routing/**`
- P11 outbound files (`services/outbound.py`, `dialer.py`, `list_import.py`) except
  calling dialer machinery for DR-6 callbacks THROUGH its public functions
- `agents/**`, `.env*`, `deploy/**`, CI config, `pyproject.toml`

## Test spec

Unit:
- [ ] engine: menu digit → branch; invalid digit retries then default; timeout →
      default; hours node open/closed/holiday (frozen clock, DST date); nested menu
      (2 levels) reaches ring_group/queue/voicemail nodes; validation gate rejects
      dangling refs and unreachable nodes with exact ids
- [ ] engine runtime error → voicemail fallback if present, else hangup (DR-4)

Integration:
- [ ] inbound carrier webhook on a bound number renders the menu prompt + Gather
      (BXML), a digit webhook advances the flow, second menu level reached, terminal
      Transfer/voicemail commands correct; unbound number keeps today's default
- [ ] room inbound with ring_group: targeted `call.ring` carries member ids;
      sequential group advances on tick after timeout; claim cancels remaining offers
- [ ] queue: entry `waiting` → offer to ring group on tick → `connected` on claim;
      caller hangup → `abandoned`; max_wait → overflow action runs (each of the three);
      position derived correctly with 3 waiting calls
- [ ] callback: digit during queue → `callback_requested` + event + queryable list
- [ ] voicemail: greeting + StartRecording commands; `recording_ready` → voicemails
      row links recording; sweeper with no DEEPGRAM_API_KEY → `disabled`; with a
      mocked Deepgram transport → transcript stored, `done`
- [ ] supervisor: monitor token has canPublish=false; whisper calls
      update_subscriptions denying the caller identity (fake LiveKit API records it);
      barge full token; each writes its VoiceEvent; non-supervisor RBAC-denied

Manual (live, BLOCKED on B2 — record as runtime-blocked):
- [ ] the PHASES.md gate sentence end-to-end on a real call

Pass criteria: full backend suite green + ruff + frontend suite + OpenAPI drift gate;
CI green after push.

## Deploy

yes — migrate + restart api; live gate recorded as blocked on B2.
