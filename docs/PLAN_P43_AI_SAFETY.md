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
At dispatch (every send path): exempt auto-replies → cached verdict per normalised body → rules
block clear scams → AI allow/hold/block vs declared business → outage: hold new/flagged
accounts, send + re-check later for established ones. Held texts get a stricter second AI look;
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
Public `/report` page.

**Slice 6 — proof it works** `services/monitor_exam.py`, `scripts/monitor_exam.py`
38-case library (texts + calls) plus operator labels, run through the live judgement functions.
Live result 2026-09-17: 19/19 scams caught, 0/19 false alarms. Hourly canary and weekly exam
record `monitor_health`; a failure opens a `monitor_health` alert. `/ops` Monitoring tab shows
health, daily numbers, flagged accounts, held texts.

## Tests
`test_p43_auth_fixes.py`, `test_p43_kyc_automation.py`, `test_p43_text_guard.py`,
`test_p43_call_monitoring.py`, `test_p43_monitoring_ops.py`, updated P41/P42 suites,
`agents/tests/test_worker_config.py`, frontend `P43Monitoring.test.tsx`. Tests use a fake
safety AI (`tests/fake_ai.py`); migrations 0050/0051 verified up/down/up on Postgres.

## Not done / limits
- Paid US registry: none under $0.50/lookup was confirmed; a provider can be added behind
  `kyc_checks.check_registry` if a quote meets the budget.
- Inbound carrier calls are not recorded for monitoring (flows control recording); inbound
  softphone calls are.
- The call-monitor worker was import-checked against livekit-agents 1.7 but needs a live
  LiveKit call to verify end to end (RUNBOOK step).
- No AI can catch everything; the exam, canary and operator labels exist to keep misses rare
  and visible.
