# P43 — AI safety: bug fixes, hands-off verification, traffic monitoring

Built 2026-09-17 on branch `p41-kyc`. Migrations `0050_kyc_automation`, `0051_monitoring`.

## Why
- A debug pass on P41/P42 found 13 real auth/verification bugs (1 critical).
- Business reviews can't be done by hand: the AI must prepare every decision, a human only
  clicks. An ID's address is often out of date, so owners prove where they live.
- Verified, legitimate-looking businesses can still hide scam calls and texts inside normal
  traffic. The AI has to watch traffic automatically, and we need proof it keeps working.

## Decisions (operator, 2026-09-17)
| # | Decision |
|---|---|
| D-P43-1 | Safety AI: DeepSeek Flash (`deepseek-flash`) via api.deepseek.com, platform key. |
| D-P43-2 | Data goes to DeepSeek's own API; DeepSeek must be listed as a processor. |
| D-P43-3 | AI prepares the KYC decision (review, thinking, recommendation); an operator approves. AI never approves. |
| D-P43-4 | No video call; high-risk applications need every document to fully match. |
| D-P43-5 | Every owner uploads proof of residence dated within 90 days. |
| D-P43-8 | 2026-09-17: US and UK only for now (`KYC_COUNTRIES=US,GB`); Canada code kept but switched off. |
| D-P43-6 | Registry lookups only if <= $0.50 each: free sources only (Companies House, Corporations Canada, NY/CO/OR/CT open data, otherwise AI-read registration documents). |
| D-P43-7 | Monitoring may hold texts and pause accounts automatically; suspending/banning stays human. |

## Built

**Slice 0 — bug fixes** (`tests/test_p43_auth_fixes.py`, one test per finding)
SSO/`trust_idp_mfa` owner-only with step-up · no owner role via SSO/SCIM default · passkey or
authenticator add/remove needs fresh 2FA · approval re-screens sanctions/ban list/name match ·
re-verification must be the same person · API key rotate/revoke scope check · events socket
re-checks session/membership/policy · lockout no longer a password oracle, email-case-safe
rate limit · `trust_idp_mfa` per workspace · reset rate limit per token · TOTP step-up counts
toward lockout · atomic step-up consumption · operator cookie sessions on legacy ops routes.

**Slice 1 — safety AI** `services/ai_guard.py`: DeepSeek Flash, JSON mode, thinking off, images,
one retry, untrusted content always tagged as data, never fails open. `llm_client` defaults to
`deepseek-flash` with thinking disabled.

**Slice 2 — hands-off verification**
- Owner residential address + `proof_of_address` per owner (submission requires both).
- `services/kyc_doc_reader.py`: the AI reads each document (digital PDFs as text, scanned PDFs
  rendered with pypdfium2, images directly); code decides pass/warn/fail (type, name, address,
  date ≤ 90 days, company, number, signs of editing). Applicant sees the result within seconds.
- Registries: Companies House (UK), Corporations Canada federal API (`ISED_API_KEY`), NY/CO/OR/CT
  open data, else confirmed from AI-read registration documents.
- `services/kyc_decision.py`: AI decision pack, regenerated only when inputs change.
- `services/kyc_automation.py`: runs after upload/submit and every 2 minutes.
- Console: owner address + proof upload with live result; `/ops` decision pack with one-click
  approve (applies suggested limits) / ask for info (prefilled) / reject.

**Slice 3 — AI text guard** `services/monitor_text.py`, `services/monitor_rules.py`
At dispatch (every send path): the platform's unedited STOP/START/HELP replies are exempt (an
edited reply, or one whose workspace name or help contact carries a link or phone number, is
screened) → cached verdict per normalised body → rules
block clear scams → AI allow/hold/block vs declared business → outage: hold new/flagged
accounts, send + re-check later for established ones. Link tracking screens the original links
before they are replaced. The cache key keeps digits in links and phone numbers. Held texts get a stricter second AI look;
cleared texts go out through the normal release (compliance re-checked).

**Slice 4 — calls** `services/monitor_calls.py`, `agents/call_monitor.py`
Chosen: new accounts (60 days), watched accounts, 20% sample. Carrier calls record after a
mandatory announcement and are transcribed by Deepgram (diarized). Softphone room calls get the
silent `call-monitor` worker (announcement + live transcript). AI reviews transcripts with quotes.

**Slice 5 — risk score and pause** `services/monitor_score.py`, `api/routes/monitoring.py`
Signals: blocked/confirmed texts, suspicious/scam calls, short calls, no-answer rate, volume
spikes, STOP rate, carrier spam rejections, complaint replies, public reports. Levels: watch (30)
→ restricted (60, daily caps) → paused (100, telephony refused, AI case file, owners emailed,
customer can appeal). Operators unpause or suspend+ban; decisions become labels.
Public `/report` page. Outsider signals (public reports, complaint replies) are capped (2 reports
a day count; one per number+IP per day) and can reach "restricted" at most: a pause always needs
platform-observed evidence (blocked texts, scam calls, carrier data).
AI outages never stall the sweeper: every AI job has a time budget and stops calling the AI for
the rest of a pass once it fails (pauses still email owners without a case file).

**Slice 6 — proof it works** `services/monitor_exam.py`, `scripts/monitor_exam.py`
38-case library (texts + calls) plus operator labels, run through the live judgement functions.
Live result 2026-09-17: 19/19 scams caught, 0/19 false alarms. Hourly canary and weekly exam
record `monitor_health`; a failure opens a `monitor_health` alert. `/ops` Monitoring tab shows
health, daily numbers, flagged accounts, held texts.

## Tests
`test_p43_auth_fixes.py`, `test_p43_kyc_automation.py`, `test_p43_text_guard.py`,
`test_p43_call_monitoring.py`, `test_p43_monitoring_ops.py`, `test_p43_monitor_fixes.py`
(second bug hunt), updated P41/P42 suites,
`agents/tests/test_worker_config.py`, frontend `P43Monitoring.test.tsx`. Tests use a fake
safety AI (`tests/fake_ai.py`); migrations 0050/0051 verified up/down/up on Postgres.

## Live demo (2026-09-17, local, real DeepSeek)
Wrong-person 8-month-old bill → fail with 3 reasons; matching bill and certificate → pass;
decision pack recommended "needs info" (placeholder website) and nothing was approved until the
operator clicked; approved business: normal text sent, bank-phishing and IRS gift-card texts
blocked; after 4 blocked scams the account paused, a normal text was refused
(`account_paused`), the console showed the appeal banner and the AI case file quoted all four
texts. (Stripe ID was simulated: no Stripe keys locally.)

## Full US + UK run (2026-09-17, local, real DeepSeek)
Local end-to-end script (not committed), 129/129 checks: auth (2FA, lockout), country gate (US/UK only),
four applicants (Texas LLC via documents + needs-info round trip, New York LLC confirmed live by
the NY registry, UK Ltd via Companies House certificate, scam applicant: AI "reject 92%", approve
refused while blockers remain, rejected + banned), texts in both countries (cache, rules block,
AI block, tracked-link phishing, off-topic promo, scheduled scam, STOP reply exempt, opt-out),
held text (operator block from /ops, second look clears and sends), calls (US legit "ok 95",
SSA scam "scam 100", HMRC and Barclays safe-account scams "scam 100"), public reports (dedupe,
never pause), UK risk ladder (watch -> restricted daily cap -> paused, texts and calls refused,
appeal, AI case file with quotes, operator 2FA step-up, unpause, suspend + ban), daily report,
labels, canary. Exam library now 50 cases incl. 12 UK: 25/25 caught, 0/25 false alarms.
Stripe ID, carrier delivery and call audio were simulated (no local credentials).

Adversarial audit (a second session, 2026-09-17) found four more, all fixed: an open events
websocket never re-checked the PLATFORM idle timeout, so an unattended console kept streaming
events for hours after "signed out after inactivity"; the workspace login-event list returned a
shared member's sign-ins to OTHER workspaces (ip, device, risk flags, and in bulk via CSV);
`MONITOR_ENFORCED=false` silently RELEASED every already-paused account instead of only
stopping new pauses; and `test_risk_rules` was timezone-dependent (UTC vs local date), which
also affected the "incorporation date in the future" guard. Plus two SAML hardening changes
(expired IdP certificate refused at save time and surfaced as an alert at sign-in; the SAML
response bound to the browser that started it) and the Stripe Identity webhook secret no longer
accepted for billing events. The audit CLEARED the SAML core, tenant isolation across all 21
unscoped query sites, operator document access, IdP-group-to-role mapping and step-up scoping.

Bugs found and fixed by this run: UK recipients were checked against US quiet hours (daytime
texts held until the evening); the weekly exam and hourly canary held a database transaction
open for minutes; the ops queue lacked the AI recommendation; operators without a workspace
could not open /ops; digits in a workspace name were read as a phone number.

## Two decisions for the operator, now that GB is live (not code bugs)
- **The rate card has no destination dimension.** `spend.resolve_rate` keys on (provider,
  metric) only, so a text or call to +44 costs and prices exactly like a US domestic one, and
  `DEFAULT_TRAFFIC_MARKUP_BPS` is a flat multiplier on a destination-blind cost. UK domestic
  and international termination rates differ substantially, so the margin on UK traffic is
  whatever the difference happens to be. Adding a destination dimension is real work.
- **The platform is single-currency by construction.** `ProviderRate.currency` is USD, the
  billing models carry no currency column, and nothing converts. Setting
  `STRIPE_PRICE_CURRENCY=gbp` would therefore charge USD-derived amounts labelled GBP - so
  that configuration is now REFUSED at boot unless `ALLOW_NON_USD_PRICING=true` is set
  deliberately. Staying single-currency is a fine decision; doing it by accident is not.

## Not done / limits
- Paid US registry: none under $0.50/lookup was confirmed; a provider can be added behind
  `kyc_checks.check_registry` if a quote meets the budget.
- Inbound carrier calls are not recorded for monitoring (flows control recording); inbound
  softphone calls are.
- The call-monitor worker was import-checked against livekit-agents 1.7 but needs a live
  LiveKit call to verify end to end (RUNBOOK step).
- No AI can catch everything; the exam, canary and operator labels exist to keep misses rare
  and visible.
