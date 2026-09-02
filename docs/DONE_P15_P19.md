# DONE — Phases 15–19 (2026-09-01 → 2026-09-03)

The approved follow-on plan (`docs/PLAN_P15_P19.md`) is complete and deployed to
https://csaas.sabinepropertygroup.net. Migrations through `0020`.

## What was built
- **P15 Tiered inboxes** — departments, one inbox per number, member/viewer grants,
  admin bypass; fail-closed enforcement on threads, messages, every call-control
  route, recordings, voicemails, supervisor monitor/whisper/barge, WS ring/handoff.
- **P16 Quo-style inbox** — dark three-pane layout, per-number inboxes in the sidebar,
  Chats/Calls tabs + Open/Unread/Unresponded, unified SMS + call + voicemail timeline
  per contact (failed calls show the carrier reason), contact panel, departments/grants
  admin UI. Agent role gained `calls:read`.
- **P17 Provider accounts in-app** — Bandwidth/Telnyx/Twilio/Plivo/SignalWire
  credentials pasted in the Providers page, Fernet-encrypted (`CREDENTIALS_MASTER_KEY`),
  probed, and served through a transparent per-org carrier registry proxy; messaging
  webhooks verify against DB accounts; empty env webhook creds never verify.
- **P18 Number purchasing** — search/buy/release at all five providers (Bandwidth XML
  + SignalWire LaML added; Telnyx order polling), costs captured at purchase, async
  orders polled by the sweeper with org registry priming, Numbers page across providers.
- **P19 Cost tracking** — rate cards (org overrides over code defaults), daily spend
  derived in SQL from billable segments / minutes / number MRC + setup, spend per
  provider/metric/number, rates drawer, dashboard 30-day chart, Numbers MTD column.

## Quality gates
Backend 1056 tests, frontend 142. Every phase: DeepSeek V4 Pro draft → Sonnet
integration → Opus adversarial review (mutation probes) → Fable sign-off → deploy →
live checks. Opus found and we closed ~20 blockers across the five phases (ungated
call control, proxy dunders, empty-env webhook bypass, env-registry polling, SMS
counted per message, …). Residuals are ledgered in `docs/OPEN_ISSUES.md` (D30–D38).

## Known limitations
- Calls still blocked by the Bandwidth FREE TRIAL (402 to any non-verified number);
  the SIP peer (B2) for inbound is a portal step. Telnyx keys can now be pasted
  in-app; AI keys still go in `.env`.
- Bandwidth Numbers API auth (Basic with the API user pair) is unverified live (D37).
- Voice webhooks have no DB-account fallback yet (D35); carrier catalog mixes env
  and DB truth (D36); legacy pages stay light-themed beside the dark inbox.
- Spend is an estimate from our own records × list-price defaults — edit rates.

## Next steps (not started)
1. Live smoke on a paid Bandwidth account: search → order → poll → inbox → call.
2. D35 voice webhook fallback; D36 account-aware carrier catalog; sidebar department
   grouping (needs `department_id` on inboxes); global dark theme.
3. Spend reconciliation against provider invoices/CDRs (P19 is estimate-only).
