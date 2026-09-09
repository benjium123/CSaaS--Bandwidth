# Plan P22–P25 — Enterprise tier: contact ownership, custom roles, AI voice agent product, AI metering & charging, enterprise identity

Author: Fable (Tier 1), 2026-09-10. Status: proposed, awaiting user approval.
Ordering: the user asked for contact ownership + custom roles FIRST. P22 has a small UI
footprint, so it can run before P20 (simplicity) without conflict; P23–P25 should follow
P20 because their admin surfaces belong in the P20 Settings layout. P21 (smart routing)
is unchanged and independent.

Numbering: migrations continue from 0023 (`0024` P22, `0025` P23, `0026` P24, `0027`
P25). Fable writes every migration and every ledger/money table. Implementers get the
Allowed-Files lists below and nothing else.

Vocabulary rule (from P20): customer-facing words only. "Owner", "team", "role",
"assistant" (not "agent profile"), "credits", "rate". No provider names on working pages.

---

## Current state this plan builds on (verified in code 2026-09-10)

- Contacts: `contacts` is org-scoped only. No owner / creator / department column.
  Routes gate on `contacts:read` / `contacts:write`, which every system role holds, so
  every member sees every contact. Threads DO have `assigned_user_id`; that is
  conversation assignment, not contact ownership.
- Roles: `roles` table already stores a JSON permission list + `is_system`, and
  `roles:read` / `roles:write` permissions exist, but nothing creates non-system roles
  (no routes, no UI). System roles: owner (`*`), admin (all but org:delete/org:billing),
  agent (inbox + contacts + read-only compliance/templates/calls).
- Departments: `departments`, `department_members`, `inbox_grants` (P15). No "lead"
  flag on membership.
- AI: `agent_profiles` (prompt, greeting, voice_id, llm_provider/model, voicemail text,
  SMS settings, `extra` JSON), `call_transcripts`, `agent_sms_turns` (tokens_in/out),
  `call_scores` (tokens_in/out added 2026-09-09), KB search, appointments, handoff, AMD
  endpoints under `/agent/*`. STT/TTS/LLM run in the EXTERNAL voice worker; all AI keys
  are platform-wide env vars in `config.py` (anthropic, openai, deepseek, groq, google,
  deepgram, assemblyai, elevenlabs, cartesia). No per-org AI keys.
- Money: `provider_rates` (provider, metric, unit_cost_micros) and
  `provider_spend_daily` (P19) exist for CARRIER spend only. `USAGE_METRICS` already
  lists `ai_sms_turns` and `ai_tokens` but nothing bills them. No credit ledger, no
  Stripe, no sell-side price anywhere.
- P17 pattern to reuse: `provider_accounts` (per-org Fernet-encrypted credentials,
  field catalogue with secret flags, probe → status pill). AI providers get the SAME
  UX but a SEPARATE table so the carrier registry is untouched.

---

# Phase 22 — Contact ownership, visibility, and custom roles

## Goal
A contact has an owner and a team. Who can see a contact is an org policy (everyone /
my team / mine), enforced in the API, not just hidden in the UI. Admins can create
their own roles from a permission matrix instead of choosing between "admin" and
"agent". This is the "manager sees their team's contacts" story enterprise buyers ask
for first.

## Pre-dependencies
None new. Migration `0024_contact_ownership_roles` (Fable).

## Schema (migration 0024, Fable-only)
- `contacts.owner_user_id` UUID NULL FK users ON DELETE SET NULL; index
  `(org_id, owner_user_id)`.
- `contacts.department_id` UUID NULL FK departments ON DELETE SET NULL; index
  `(org_id, department_id)`.
- `department_members.is_lead` BOOL NOT NULL DEFAULT false.
- `orgs.contact_visibility` VARCHAR(16) NOT NULL DEFAULT `'everyone'`
  (`everyone` | `department` | `owner`). Default preserves today's behaviour exactly.
- Backfill: none for contacts (unowned is a valid state). Roles: no schema change;
  `roles.permissions` JSON already carries custom sets.

## Permissions (additions to `PERMISSIONS`)
- `contacts:read_all` — bypass the visibility policy (see every contact).
- `contacts:assign` — change a contact's owner/department.
- Backfill via migration 0024: append both to every existing `admin` system role row
  (owner has `*`). Agents get neither.

## Visibility rule (single function, `services/contact_visibility.py`)
`visible_contacts_filter(ctx) -> SQL predicate`, applied in EVERY contacts query
(list, get, search, phone lookup, merge, notes, tags, the inbox contact card, and the
`/agent/contact/{e164}` lookup used by the AI worker — the worker resolves as the
inbox's department, never as read_all):
- `everyone`, or caller holds `contacts:read_all` → no predicate.
- `department` → owner_user_id = me OR department_id IN (my departments) OR
  (department_id IS NULL AND owner_user_id IS NULL AND I hold contacts:write) ...
  the last clause keeps unowned, unteamed contacts workable rather than orphaned.
- `owner` → owner_user_id = me OR (I am `is_lead` of department_id) OR unowned+unteamed
  as above.
Fail-closed: any contact that the predicate does not match returns 404 (not 403), so
existence is not leaked across teams.

Inbox interaction (decision, do not relitigate): the inbox grant governs the THREAD;
the contact policy governs the CONTACT RECORD. An agent who can see a thread sees the
contact's display name and phone in the thread header (they already see the phone),
but the "Open contact" link 404s if the contact policy excludes them. Document this in
Settings copy: "Team members always see who they are talking to; the contact's full
record follows the visibility rule."

## Ownership assignment
- On manual create: owner = creator; department = creator's first department (or
  NULL).
- On inbound auto-create (unknown number texts/calls in): owner = the thread's
  `assigned_user_id` if set, else NULL; department = the inbox's department if the
  inbox has one, else NULL.
- `PATCH /api/v1/contacts/{id}/owner {owner_user_id?, department_id?}` requires
  `contacts:assign`; owner must be an org member; department must be an org
  department.
- `POST /api/v1/contacts/bulk/assign {contact_ids[], owner_user_id?, department_id?}`
  (max 500 ids per call, `contacts:assign`).
- Import (`services/list_import.py`): new optional "Owner" column (email) and an
  "Assign all to" picker in the import UI; unknown emails → row imported unowned and
  reported in the import summary, never rejected.
- Audit: every ownership change writes an `AuditLogEntry` (`contact.assign`).

## Custom roles
- Routes (`routes/roles.py`, permissions `roles:read` / `roles:write`):
  `GET /roles`, `POST /roles` (name, permissions[], optional `clone_from` role id),
  `PATCH /roles/{id}` (name, permissions), `DELETE /roles/{id}`.
- Guards (all tested):
  1. System roles (`is_system`) cannot be edited or deleted (409).
  2. A role that is assigned to any member cannot be deleted (409 with count).
  3. Privilege escalation: the caller may only grant permissions the caller holds.
     `*` can never be granted to a custom role. `org:delete` and `org:billing` are
     owner-only and are rejected on custom roles.
  4. `validate_permissions()` already rejects unknown keys — keep it.
  5. Changing a member's role to a custom role uses the existing members:update path;
     a member cannot change their own role.
- UI (`TeamPage` → new "Roles" tab; P20 later moves it under Settings → Team):
  list of roles with member counts; "New role" → pick a starting point (Agent /
  Admin / blank) → permission matrix grouped by resource with plain-word labels
  ("Can see contacts", "Can see all contacts (bypasses team rule)", "Can reassign
  contacts", "Can send & call", "Can supervise live calls", ...). Save → pill.
  Permissions the current user does not hold render disabled with a tooltip.
- Settings → Workspace (or the existing Security page until P20): "Contact
  visibility" radio: Everyone / Their team / Only the owner (plus team leads), with
  one sentence of consequence under each.
- Contacts page: Owner and Team columns, filter chips "Mine" / "My team" / "Unowned",
  assign drawer (single and bulk via checkbox selection).

## Allowed files (implementer: Sonnet in-repo; DeepSeek V4 Pro may draft the matrix UI)
Backend: `backend/app/models/contacts.py` (two columns only), `backend/app/models/inboxes.py`
(`is_lead`), `backend/app/models/org.py` (`contact_visibility`), `backend/app/models/rbac.py`
(two permission keys + agent/admin lists), `backend/app/services/contact_visibility.py` (new),
`backend/app/api/routes/contacts.py`, `backend/app/api/routes/roles.py` (new),
`backend/app/api/routes/departments.py` (lead flag), `backend/app/api/routes/orgs.py`
(visibility setting), `backend/app/api/routes/agent.py` (contact lookup scoping only),
`backend/app/services/list_import.py` (owner column), `backend/app/schemas/*` for the above,
`backend/tests/test_p22_*.py`.
Frontend: `frontend/src/pages/ContactsPage.tsx`, `frontend/src/pages/TeamPage.tsx`,
`frontend/src/pages/SettingsSecurityPage.tsx`, `frontend/src/api/contacts.ts`,
`frontend/src/api/roles.ts` (new), `frontend/src/components/contacts/*` (new),
`frontend/src/components/team/RoleMatrix.tsx` (new), matching `*.test.tsx`.
Forbidden: migrations (Fable writes 0024), `backend/app/auth/*`, `deploy/**`, `.env`,
inbox grant logic in `services/inbox_access.py` (read it, do not change it).
Scope: ≤ 18 files; no new dependencies.

## Test spec
Unit:
- [ ] visibility_everyone_returns_no_predicate → list is identical for agent and admin.
- [ ] visibility_department_hides_other_team → agent A (dept X) cannot GET a contact
      owned by agent B (dept Y): 404, and it is absent from list/search/phone lookup.
- [ ] visibility_owner_lead_sees_team → lead of dept X sees contacts of every member of X.
- [ ] read_all_bypasses_policy → admin sees everything under every policy.
- [ ] unowned_unteamed_visible_to_writers → under `department`, a contact with no owner
      and no team is visible to any contacts:write holder.
- [ ] inbound_autocreate_sets_department_from_inbox and owner_from_thread_assignee.
- [ ] assign_requires_permission (403 for agent), assign_rejects_non_member (422).
- [ ] bulk_assign_caps_at_500 (422 above).
- [ ] agent_contact_lookup_scoped_to_inbox_department (the `/agent/contact/{e164}` path).
- [ ] roles_create_clone_from_agent, roles_edit_system_role_409, roles_delete_in_use_409.
- [ ] roles_escalation_blocked → admin (no org:billing) cannot create a role with
      org:billing; agent with roles:write via a custom role cannot grant calls:supervise.
- [ ] roles_wildcard_rejected, roles_unknown_key_422.
- [ ] members_cannot_change_own_role.
- [ ] import_owner_column_maps_email_and_reports_unknowns.
Integration:
- [ ] Full flow: org sets `owner` policy → agent creates contact → lead sees it, peer
      does not → admin reassigns to peer → peer sees it, original owner does not.
- [ ] Custom role "Team lead" (contacts:read + contacts:assign, no read_all) can reassign
      inside their team only (the assign target must be visible to them).
Frontend:
- [ ] Contacts page renders Owner/Team columns and Mine/My team/Unowned chips; assign
      drawer submits and shows success.
- [ ] RoleMatrix disables permissions the user lacks; system roles are read-only.
- [ ] Visibility radio saves and shows the consequence copy.
Manual: log in as an agent in a two-department org and confirm the 404 on a foreign
contact's URL, then as the lead.
Pass criteria: all green; backend baseline 1210 must not regress.

## Deploy
yes (migration 0024 is additive; default policy `everyone` means zero behaviour change
until an admin flips it).

---

# Phase 23 — AI voice assistant as a product (features + in-app AI provider keys)

## Goal
Turn the internal AI agent into a customer-configurable assistant: a builder with
persona, goals, guardrails, knowledge, tools, and voice; usable on inbound numbers and
outbound campaigns; with post-call outcomes flowing into contacts and the inbox. Orgs
either bring their own AI keys (BYOK) or use platform keys and pay per use (P24).

## Pre-dependencies
- Voice worker repo: it must accept a per-call resolved config (keys + model + voice)
  from `GET /agent/context/{call_id}` instead of reading env. That change is in the
  external worker, tracked separately; the API side ships here behind a feature flag
  `AI_PER_ORG_KEYS=1` so the env fallback keeps working until the worker is updated.
- Migration `0025_ai_assistant_product` (Fable).

## Schema (migration 0025, Fable-only)
- `ai_provider_accounts` — mirrors `provider_accounts`: id, org_id, kind
  (`llm`|`stt`|`tts`), provider (llm: openai, anthropic, deepseek, groq, google; stt:
  deepgram, assemblyai; tts: elevenlabs, cartesia), label, credentials_encrypted,
  status (unverified/active/failed/disabled), last_probe_at/detail, created_by.
  Unique (org_id, kind, provider, label).
- `orgs.ai_key_mode` VARCHAR(8) NOT NULL DEFAULT `'platform'` (`platform` | `byok`).
- `agent_profiles` additions: `goals` TEXT, `guardrails` TEXT, `language` VARCHAR(8)
  DEFAULT 'en', `stt_provider`, `tts_provider` VARCHAR(16), `max_call_seconds` INT
  DEFAULT 900, `silence_timeout_seconds` INT DEFAULT 12, `interrupt_sensitivity`
  VARCHAR(8) DEFAULT 'medium', `voicemail_action` VARCHAR(16) DEFAULT 'leave_message'
  (`leave_message`|`hang_up`|`retry_later`), `tools` JSON DEFAULT '[]',
  `post_call_fields` JSON DEFAULT '[]'.
- `kb_documents` — org_id, title, source (`upload`|`url`|`text`), storage_key, status
  (`pending`|`indexed`|`failed`), chunk_count, created_by. (KB search exists; this adds
  ingestion. Vector store stays whatever P8 uses; if it is in-process, keep it.)
- `call_outcomes` — call_id (unique), profile_id, summary TEXT, disposition VARCHAR(32),
  sentiment VARCHAR(8), intent VARCHAR(64), extracted JSON, handoff BOOL, booked BOOL,
  follow_up_sms_message_id NULL. One row per AI call, written by the worker batch.
- `campaigns.agent_profile_id` NULL FK (AI outbound campaigns).
- Call-flow engine: new node type `ASSISTANT {profile_id}` — no schema change if flow
  definitions are JSON (they are); add the node to the validator.

## Features (customer-visible names in quotes)
1. "AI providers" (Settings → AI → Providers): same UX as P17 — pick provider → only
   its fields → Save → Probe → status pill. Mode switch: "Use CSaaS keys (billed per
   use)" vs "Use my own keys". In BYOK mode every kind (llm, stt, tts) must have an
   active account before an assistant can go live; the assistant editor shows what is
   missing. Probe = one cheap real call per provider (list models / list voices /
   1-second transcription of a bundled WAV).
2. "Assistants" builder (rework of `AgentPage`): tabs Persona (name, greeting,
   language, voice with a 3-second preview via the TTS provider), Instructions (system
   prompt, goals, guardrails as separate fields merged server-side with a fixed
   compliance preamble the customer cannot remove: identify as automated when asked,
   honour STOP/do-not-call, no medical/legal/financial advice), Knowledge (upload
   PDF/DOCX/TXT/URL → indexed → searchable; existing `/agent/kb/search`), Tools
   (toggles: Book appointment, Transfer to human/queue, Send follow-up text, Look up
   contact, Custom webhook {url, secret, input schema} — the worker calls
   `/agent/tools/{tool}` server-side so secrets never reach the worker), Behaviour
   (max duration, silence timeout, interruptions, voicemail action), Outcomes
   (post-call fields to extract: name, type text/number/date/select, "write to contact
   attribute X" mapping).
3. "Test your assistant": text simulator in the editor (`POST /agent/simulate` runs
   one LLM turn with the merged prompt + KB; no telephony) and "Call me" (places an
   outbound call from an org number to the admin's verified phone with this profile,
   using the existing dialer path; flagged as test in `call_outcomes`).
4. Use on inbound: flow node "Assistant" in Flows; Numbers page "Answered by:
   Human / Assistant X" shortcut that writes a one-node flow.
5. Use on outbound: campaign type "AI calls" → pick assistant, list, schedule, quiet
   hours and 10DLC/consent gates apply exactly as for human dialer campaigns (4.1 fix
   from the bug-fix round is the gate; do not bypass).
6. Post-call: `POST /agent/outcome` (worker, batch, idempotent on call_id) stores
   summary/disposition/sentiment/extracted; extracted fields with a mapping write to
   `contacts.attributes`; "Send follow-up text" tool result links the SMS; the inbox
   timeline shows an "AI call" card with summary + Play + transcript expander (P16
   timeline already renders calls; add the card body).
7. Handoff: existing `/agent/handoff` → warm transfer to a queue/user; the inbox thread
   gets assigned to the receiving user (ties into P22 ownership: the contact's owner
   becomes that user if unowned).
8. Analytics (Dashboard tile + Assistants page): AI calls, minutes, answer rate,
   handoff rate, booked, avg duration, cost per call (from P24 when live, else "—").

## Allowed files
Backend: `backend/app/models/agent.py` (columns listed), `backend/app/models/provider_accounts.py`
(NO changes — new model goes in `backend/app/models/ai_providers.py`),
`backend/app/models/org.py` (`ai_key_mode`), `backend/app/models/outbound.py`
(`agent_profile_id`), `backend/app/services/ai_providers.py` (new: registry, probe, resolve
per-call config, BYOK completeness check), `backend/app/services/kb_ingest.py` (new),
`backend/app/services/agent.py`, `backend/app/services/flow_engine.py` (ASSISTANT node),
`backend/app/services/dialer.py` (profile on campaign), `backend/app/api/routes/agent.py`,
`backend/app/api/routes/ai_providers.py` (new), `backend/app/api/routes/flows.py` (validator),
`backend/app/api/routes/outbound.py` (campaign type), schemas, `backend/tests/test_p23_*.py`.
Frontend: `AgentPage.tsx` (becomes Assistants), `frontend/src/components/assistants/*` (new),
`frontend/src/pages/ProvidersPage.tsx` (AI tab) or the P20 Settings → AI section if P20 has
landed, `FlowsPage.tsx` (node), `CampaignsPage.tsx` (AI type), `NumbersPage.tsx` (Answered by),
inbox timeline card component, `frontend/src/api/assistants.ts` (new), tests.
Forbidden: migrations, `services/credentials.py` (reuse `encrypt/decrypt` as-is),
`carriers/**`, `.env`, `deploy/**`, the voice worker.
Scope: this is the largest phase; split into P23a (providers + builder + simulate) and
P23b (inbound/outbound wiring + outcomes + analytics). ≤ 25 files each. New dependency
requests (PDF/DOCX text extraction) go to Fable first.

## Test spec
Unit:
- [ ] ai_provider_create_encrypts_and_never_echoes_secret; probe_failure_sets_failed.
- [ ] byok_mode_blocks_go_live_without_all_three_kinds (422 lists the missing kinds).
- [ ] resolve_call_config_platform_mode_uses_env; byok_mode_uses_org_keys; keys never
      appear in `/agent/context` JSON when the worker flag is off.
- [ ] compliance_preamble_is_always_prepended_and_not_editable.
- [ ] simulate_returns_one_turn_and_meters_tokens (writes usage rows for P24).
- [ ] kb_ingest_chunks_and_marks_indexed; failed_parse_marks_failed_not_500.
- [ ] tool_webhook_called_server_side_with_hmac; worker_never_receives_tool_secret.
- [ ] flow_assistant_node_validates_profile_belongs_to_org.
- [ ] ai_campaign_respects_quiet_hours_consent_and_10dlc_gate (reuse the 4.1 tests).
- [ ] outcome_post_is_idempotent_on_call_id; extracted_fields_write_contact_attributes
      only for mapped keys; unmapped keys are stored, not applied.
- [ ] handoff_assigns_thread_and_sets_unowned_contact_owner.
- [ ] test_call_is_flagged_and_excluded_from_analytics.
Integration:
- [ ] Inbound number → Assistant flow → transcript + outcome → contact updated → card in
      inbox timeline (stub worker via the existing test fake).
- [ ] AI campaign of 3 contacts: one answered (outcome), one voicemail (voicemail_action
      honoured), one DNC (never dialled).
Frontend:
- [ ] Builder tabs save independently; Knowledge upload shows pending → indexed.
- [ ] Simulator renders a turn; "Call me" is disabled without a verified phone.
- [ ] Providers AI tab: field set switches per provider; secrets masked after save.
Manual: one real inbound call to the assistant with two-way audio; one "Call me".
Pass criteria: all green; the human-call path (LiveKit SIP) untouched — re-run the
inbound/outbound two-way-audio check from the 2026-09-09 deploy.

## Deploy
yes (P23a can deploy on its own; the worker update gates BYOK going live).

---

# Phase 24 — AI metering, pricing, credits, and charging

## Goal
Every AI minute, transcription second, spoken character, and token is metered per org,
priced from a rate sheet with a platform margin, and drawn down from a prepaid credit
balance topped up by card. Calls stop when credits run out. BYOK orgs pay a platform
fee per minute instead of pass-through. This is money: Fable owns the ledger.

## Pre-dependencies
- Stripe account + keys (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`) in `.env` — user
  adds by hand. New dependency `stripe` (Python) — needs explicit approval.
- Migration `0026_ai_billing` (Fable).
- P23a merged (usage sources exist).

## Schema (migration 0026, Fable-only)
- Extend `USAGE_METRICS`: `ai_voice_seconds`, `stt_seconds`, `tts_characters`,
  `llm_tokens_in`, `llm_tokens_out` (keep `ai_tokens` for back-compat, stop writing it).
- `ai_usage_events` — org_id, call_id NULL, thread_id NULL, profile_id, provider, kind,
  metric, quantity, cost_micros (what we pay, from `provider_rates`), price_micros (what
  the customer pays), occurred_at, source (`worker`|`simulate`|`sms_agent`),
  idempotency_key UNIQUE. Append-only.
- `provider_rates`: extend `provider` domain to AI providers; add `price_micros`
  (sell rate) and `scope` (`traffic` | `ai`). Platform-level defaults live in rows
  with org_id = the platform org (existing platform_ops pattern); per-org overrides
  win.
- `orgs.ai_markup_bps` INT NULL (per-org override of the platform default markup, e.g.
  3000 = 30%); `orgs.ai_platform_fee_per_minute_micros` for BYOK.
- `credit_ledger` — org_id, entry_type (`topup`|`usage`|`adjustment`|`refund`|
  `reserve`|`release`), amount_micros (signed), balance_after_micros, reference
  (stripe payment intent / usage event id / admin note), created_by, created_at.
  Balance = last row's balance_after; a nightly job asserts SUM(amount) == balance.
- `orgs.credit_auto_recharge` JSON NULL ({threshold_micros, amount_micros,
  payment_method_id}).
- `payment_methods` — org_id, stripe_customer_id, stripe_pm_id, brand, last4, is_default.

## Rules
- Metering: the worker posts `POST /agent/usage` batches (idempotent). SMS agent turns
  and simulator turns write events in-process. Pricing happens at write time:
  `price = cost * (1 + markup)` in platform mode; `price = platform_fee * seconds/60`
  for BYOK voice, 0 for BYOK tokens/characters.
- Reserve at call start: estimated max cost = `max_call_seconds` × blended per-second
  price; write a `reserve` ledger entry; if balance − reserves < estimate → refuse the
  call with a customer-readable reason ("Add credits to keep your assistant answering").
  On call end: `release` the reserve and post `usage`. Reserves older than 2× max
  duration are auto-released by the sweeper.
- Warnings: 20% and 5% of the last top-up → in-app banner + email to billing contacts;
  0% → assistants answer with the human flow fallback (configurable: fallback flow or
  busy tone), campaigns pause.
- Top-up: Stripe Checkout (hosted page, no card fields in our UI) → webhook
  `payment_intent.succeeded` → `topup` ledger entry (idempotent on intent id). Auto-
  recharge uses a saved payment method via PaymentIntent off-session.
- Refunds/adjustments: platform ops only (`platform_ops_token` path), always with a
  note, always a ledger row — never an UPDATE.
- Carrier spend (P19) stays informational for now; only AI usage is charged in P24.
  (Charging for SMS/voice traffic is a later phase; the ledger is designed for it.)

## UI (Settings → Billing & usage)
Balance card + "Add credits" (25/50/100/custom) + auto-recharge toggle; Usage this
month by metric with a per-call drill-down drawer (each AI call: minutes, tokens,
price); Rate sheet (customer view: prices only, never our cost); Payment methods.
Platform ops page: default markup, per-provider cost rates, per-org overrides,
adjustments with a note, margin report (price − cost by day).

## Allowed files
Backend: `backend/app/models/platform.py` (USAGE_METRICS), `backend/app/models/spend.py`
(rate columns), `backend/app/models/billing.py` (NEW — Fable writes the ledger model;
implementer may add read-only helpers), `backend/app/services/ai_usage.py` (new),
`backend/app/services/credits.py` (new; reserve/release/usage/topup — Fable reviews every
line), `backend/app/services/stripe_client.py` (new), `backend/app/api/routes/billing.py`
(new), `backend/app/api/routes/webhooks.py` (stripe endpoint), `backend/app/api/routes/agent.py`
(usage endpoint + reserve check), `backend/app/api/routes/platform.py` (ops rates/markup),
schemas, `backend/tests/test_p24_*.py`.
Frontend: `frontend/src/pages/BillingPage.tsx` (new), `frontend/src/components/billing/*`,
`PlatformPage.tsx` (ops section), `frontend/src/api/billing.ts`, tests.
Forbidden: migrations, `credit_ledger` writes anywhere except `services/credits.py`,
`.env`, `deploy/**`. Scope ≤ 20 files; the `stripe` dependency requires approval.

## Test spec
Unit:
- [ ] usage_event_idempotent_on_key; price_uses_org_markup_override_then_platform_default.
- [ ] byok_voice_charges_platform_fee_only; byok_tokens_price_zero.
- [ ] reserve_refuses_when_insufficient (customer-readable reason); reserve_then_release
      leaves balance unchanged; usage_posts_after_release.
- [ ] stale_reserve_auto_released_by_sweeper.
- [ ] ledger_balance_after_is_monotonic_chain; nightly_integrity_check_flags_drift.
- [ ] stripe_webhook_topup_idempotent_on_intent_id; bad_signature_401; unknown_event_204.
- [ ] auto_recharge_triggers_once_below_threshold (no double charge under concurrent
      usage posts — use the SQLite StaticPool-safe pattern: commit per row).
- [ ] warnings_fire_at_20_and_5_percent_once_per_topup_cycle.
- [ ] zero_balance_fallback_flow_used_and_campaigns_paused.
- [ ] adjustments_require_platform_ops_and_note.
- [ ] rate_sheet_customer_view_never_includes_cost_micros.
Integration:
- [ ] Simulated AI call end-to-end: reserve → worker usage batch → release → ledger shows
      one usage row with price = cost × 1.3 (default markup) → drill-down matches.
- [ ] Top-up via a signed test webhook → balance increases → refused call now proceeds.
Frontend:
- [ ] Balance card, Add-credits redirect stub, usage table with drawer, rate sheet.
Manual: one real Stripe test-mode top-up; one AI call priced correctly in the drawer.
Pass criteria: all green; Fable signs off the ledger code line by line before merge.

## Deploy
yes (Stripe keys must be in `.env` first; ship with `AI_BILLING_ENFORCE=0` for one
week of shadow metering, then flip to 1).

---

# Phase 25 — Enterprise identity and controls

## Goal
The controls an enterprise security questionnaire asks for: sessions you can see and
revoke, login history, org-enforced 2FA, IP allowlists, SSO via OIDC, and an audit log
that covers everything P22–P24 added.

## Pre-dependencies
- Migration `0027_enterprise_identity` (Fable). New dependency for OIDC (`authlib`)
  needs approval.

## Schema (migration 0027)
- `sessions` — id, org_id NULL, user_id, refresh_token_hash, created_at, last_seen_at,
  ip, user_agent, revoked_at, revoked_by. Access tokens carry `sid`; the auth
  dependency rejects revoked sids (cached 60 s in Redis to avoid a DB hit per request).
- `login_events` — user_id, org_id NULL, at, ip, user_agent, outcome (`ok`|`bad_password`
  |`bad_2fa`|`locked`|`sso`), detail.
- `orgs.require_2fa` BOOL DEFAULT false; `orgs.ip_allowlist` JSON DEFAULT '[]';
  `orgs.sso` JSON NULL ({issuer, client_id, client_secret_encrypted, domain,
  enforce}).

## Features
- Settings → Team → Security: "Require 2FA for everyone" (members without 2FA get a
  setup interstitial on next login, 7-day grace shown as a countdown), "Allowed IP
  ranges" (CIDR list; the current admin's IP must be inside before save — no lockouts),
  "Single sign-on" (OIDC: Google Workspace / Microsoft Entra / generic; domain-match
  auto-provisions members with a default role chosen here; `enforce` disables password
  login for that domain except owners).
- Profile → Sessions: list with device/IP/last seen, "Sign out" per session and "Sign
  out everywhere"; admins can revoke any member's sessions from Team.
- Login history (self and admin views), exportable CSV.
- Audit log coverage: every mutation in P22 (assign, roles), P23 (provider keys,
  assistant go-live), P24 (top-ups, adjustments, rate changes), P25 (all of the above)
  writes an `AuditLogEntry`; the Platform page audit view gets filters by actor/action
  and CSV export.
- Password login rate limiting already exists (2026-09-09); SSO callback gets the same
  nginx zone.

## Test spec (abbreviated; Fable expands before handoff)
- [ ] revoked_session_rejected_within_60s; sign_out_everywhere_revokes_all_but_current.
- [ ] require_2fa_forces_setup_after_grace; owners_exempt_from_sso_enforce.
- [ ] ip_allowlist_blocks_outside_cidr_403_and_logs; save_rejects_admin_own_ip_outside.
- [ ] oidc_domain_match_autoprovisions_with_default_role; mismatched_domain_rejected;
      state/nonce validated; id_token signature verified against JWKS.
- [ ] login_events_written_for_every_outcome; audit_entries_for_every_p22_p24_mutation.
Deploy: yes.

---

## Execution order and delegation
1. P22 (Sonnet in-repo; DeepSeek V4 Pro drafts `RoleMatrix` + `contact_visibility`
   from this spec; Opus verifies with mutation tests on the visibility predicate).
2. P20, then P21 (already planned).
3. P23a → P23b (DeepSeek V4 Pro drafts services; Sonnet integrates; Opus verifies; the
   worker change runs in parallel in its own repo).
4. P24 (Fable writes ledger + credits; Sonnet does routes/UI; Opus adversarial review
   on money paths — double spend, concurrent reserve, webhook replay).
5. P25 (Sonnet; Opus security review; Fable final).

Each phase: full backend + frontend suites green, Opus verdict, Fable sign-off, one
deploy, then the two-way-audio smoke and `/status` ok before the next phase starts.
