# P41 — Trust & safety: account security + business verification

Written and built 2026-09-16 on branch `p41-kyc`. Migrations `0044_account_security`,
`0045_kyc`. (P41, not P40: P39's plan reserves P40 for the SignalWire trunk spike.)

## Why
The platform resells Telnyx/SignalWire. The biggest business risk is scammers using it -
including legit-looking businesses that hide a few scam calls in normal traffic. This phase
makes identity real and expensive to fake, keeps verified accounts from being taken over or
handed off, and gives operators an off switch. Call-content scanning and traffic monitoring
are later phases; this one captures the declared use case they will compare against.

## Decisions (operator, 2026-09-16)
| # | Decision |
|---|---|
| D-P41-1 | Businesses only. Countries: US, Canada, UK. |
| D-P41-2 | Owner ID + selfie via Stripe Identity (hosted page, no ID images stored by us). |
| D-P41-3 | Video call for high-risk applicants only. |
| D-P41-4 | Deposit and starting limits decided later: fields + enforcement built, NULL = off. |
| D-P41-5 | 2FA mandatory for every user; authenticator app + passkeys; never SMS. |
| D-P41-6 | Flagged logins (outside US/CA/UK, VPN/Tor/datacenter, new device/country) are allowed after a fresh 2FA/passkey; owner emailed; operator alert. No selfie at login. |
| D-P41-7 | Selfie re-check before risky actions (payment method, limit request, bulk numbers, API keys, admin/owner grants, use-case change). |
| D-P41-8 | Owners fully verified; admin + billing members verify their own ID before using those powers on an approved business; others 2FA only. |
| D-P41-9 | Keep costs minimal: free sources where good enough (government sanctions lists, Companies House, MaxMind GeoLite2, Tor list, RDAP); paid vendors can replace any check behind the same function later. |

## Built

**Slice A — account security** (`0044`)
- `REQUIRE_2FA_ALL_USERS` gate in `auth/deps.py::get_current_user` (+ websocket); last factor cannot be removed; production refuses false.
- Passkeys: `services/passkeys.py`, `routes/passkeys.py` (register, login second factor, step-up), single-use challenges.
- `services/login_flow.py::complete_login` - one finish path for password/TOTP/passkey; `services/login_risk.py` flags + device memory + `security_alerts` + owner email (`services/mailer.py`).
- `require_step_up` / `check_step_up` / `check_org_selfie_step_up` in `auth/deps.py`; `StepUpRequiredError` carries `kind` + `action`.
- Named operators: `platform_operators`, `require_operator(role)`, `scripts/make_operator.py`; the two copies of `require_platform_operator` merged (token or admin operator).

**Slice B — business verification** (`0045`)
- Models `models/kyc.py`: profiles, persons, documents, checks, step-ups, `fraud_identifiers` (hashed ban list), `stripe_events` (webhook replay ledger); `payment_methods.card_fingerprint`.
- `services/kyc.py` lifecycle + operator decisions (audited with operator id); `services/kyc_documents.py` (magic bytes, pypdf, Fernet-encrypted at rest, operator-only download); `services/kyc_checks.py` (registry, sanctions, ban list, website + RDAP domain age, email domain, name match, AI summary); `services/sanctions.py`; `services/kyc_risk.py`; `services/kyc_step_up.py`; Stripe Identity in `services/stripe_client.py`; identity events in `routes/webhooks.py::stripe_webhook`.
- Routes: `routes/kyc.py` (customer), `routes/ops.py` (operators).

**Slice C — gate + suspension**
- `services/telephony_access.py` beside the credit gate at every outbound call site (messaging send + dispatch, outbound calls, transfers, room calls, number add/order, managed provisioning); daily limits, number cap and deposit when set.
- `services/suspension.py`; identity-gated admin/billing permissions in `require_permission`; one unverified workspace per owner; card fingerprints checked against the ban list.

**Slice D — console**: Secure-your-account screen, passkey sign-in and management, Settings > Business verification, verification banner, step-up dialog, `/ops` operator console.

**Slice E — ongoing**: `services/kyc_tick.py` in the sweeper (only when `KYC_ENFORCED`): annual re-verification with grace period, Stripe reconcile, AI summaries, daily sanctions + Tor refresh and re-screen, challenge cleanup. `.env.example` Trust & safety section, RUNBOOK go-live steps.

## Not done / follow-ups
- Mid-session country-change detection only applies to sensitive (step-up) actions, not every request.
- Card fingerprints are captured for new payment methods only; existing rows have none.
- Deposit amount and starting limits: operator to decide (D-P41-4).
- Paid replacements (Middesk/Persona for registries, a screening vendor for sanctions, IPQS for VPN) are optional upgrades behind the existing check functions.

## Tests
`tests/test_p41_account_security.py`, `tests/test_p41_kyc.py`, `tests/test_p41_kyc_tick.py`,
`frontend/src/pages/P41TrustSafety.test.tsx`. Migrations verified up/down/up on Postgres 16
(embedded) including the grandfather backfill.
