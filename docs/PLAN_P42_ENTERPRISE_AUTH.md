# P42 — Enterprise-grade authentication

Built 2026-09-16/17 on branch `p41-kyc` (after P41). Migrations `0046_credentials`,
`0047_sessions`, `0048_api_key_networks`, `0049_enterprise_sso`.

## Why
The operator chose to keep the platform's own sign-in (cost, and fraud checks wired into
every login) instead of Clerk/WorkOS/Firebase, on one condition: it must be enterprise grade.
An audit found real gaps: no password reset or change, no 2FA recovery, no lockout, tokens
in `localStorage`, a per-worker rate limiter, a spoofable client IP, SSO domains nobody
verified, OIDC-only SSO, no user sync, removed members keeping their sessions, and number
purchases missing from the audit log.

## Decisions (operator, 2026-09-16)
| # | Decision |
|---|---|
| D-P42-1 | Keep own auth. Email through Resend (plain SMTP relay, no code dependency). |
| D-P42-2 | Add SAML 2.0 and SCIM 2.0 beside the existing OIDC SSO. |
| D-P42-3 | HttpOnly secure cookie sessions with idle (30 min) and absolute (12 h) timeouts; workspaces may set stricter ones. |
| D-P42-4 | Passkey required for owners, admins, billing and platform operators (14-day grace). Agents may keep an authenticator app. |
| D-P42-5 | Redis is required in production (rate limits, SSO state, SAML replay store). |

## Built

**Slice 1 — credentials and recovery** (`0046`)
- Password policy (`services/password_policy.py`): 12+ characters, not the email, refused if in Have I Been Pwned (k-anonymity range API, padded).
- Change (`POST /auth/password/change`, ends other sessions), forgot (always 202) and reset (single-use 30-minute token, stored hashed). A reset never skips the second factor.
- 10 single-use recovery codes; using one flags the sign-in, alerts, emails.
- Lost every factor: verified people pass a Stripe Identity selfie that must match their verified identity, then a 24 h cool-down on sensitive actions. Agents: an admin resets their 2FA. Last resort: operator reset in `/ops`.
- Progressive lockout counted from `login_events` (10 failures / 15 min, escalating), emailed; operator unlock.
- Account activity log (`account_audit_log`) shown on the Security page.

**Slice 2 — sessions** (`0047`)
- Cookie `__Host-csaas_session` (HttpOnly, Secure, SameSite=Lax); value `<sid>.<secret>`, only the SHA-256 is stored. CSRF double-submit (`csaas_csrf` = HMAC of the session) on unsafe methods.
- Idle and absolute timeouts, per-workspace stricter values; rotated on login, step-up and password change; `auth_method` recorded.
- `AUTH_BEARER_COMPAT` keeps bearer JWTs working during the cut-over; API keys unchanged. Softphone WebSocket uses the cookie plus an Origin check.
- Removing a member or changing their role ends their sessions.

**Slice 3 — brute force and network trust**
- One client-IP function honouring `TRUSTED_PROXY_COUNT` (fixes spoofing via the left-most `X-Forwarded-For`).
- Redis sliding-window rate limiter (Lua) shared by all workers; nginx `limit_req` on password, recovery, passkey, SSO and SAML paths.

**Slice 4 — passkeys for privileged accounts**
- `services/passkey_policy.py`: privileged roles and operators need a passkey session (or SSO with "trust identity provider MFA") after the grace period; `passkey_required` otherwise. Grace banner in the console.
- The workspace "require 2FA" policy now counts passkeys.

**Slice 5 — enterprise SSO** (`0049`)
- Verified domains (`org_domains`): TXT `_csaas-verify.<domain>` checked over DNS-over-HTTPS; one workspace per domain. With `SSO_REQUIRE_VERIFIED_DOMAIN` on, SSO signs nobody in for an unverified domain, and SSO enforcement ignores it.
- OIDC and SAML finish through `services/sso_provisioning.py` → `login_flow.complete_login` (login risk, device memory, cookie session, audit). Group-to-role mapping never grants owner.
- SAML SP (`services/saml.py`, signxml): SP-initiated only; signed assertion or signed response with exactly one assertion; values read only from the verified element; no encrypted assertions, SHA-1 or DTDs; Issuer, Audience, Recipient, time windows, InResponseTo; each assertion ID once. Metadata, start and ACS under `/api/v1/auth/saml/{slug}/`.
- SCIM 2.0 (`/scim/v2`): Users and Groups (= roles), per-workspace hashed tokens (owner, fresh 2FA, ID step-up). Deactivate or delete removes the membership and ends sessions. Owners can't be removed or re-roled.
- Console: Verified domains, SAML single sign-on and User sync (SCIM) cards; SAML callback.

**Slice 6 — audit, email, headers, API keys** (`0048`)
- Audit: numbers added/ordered/released, invites created/revoked/accepted, SSO provisioning, SCIM changes, domain changes. Org login history includes members' sign-ins.
- Invites are emailed. Mailer sends HTML + text and refuses plaintext SMTP in production.
- Security headers on the API (nosniff, frame deny, CSP, no-store on auth, HSTS in production) and CSP + Permissions-Policy on the console.
- API keys: optional CIDR allowlist, `last_used_ip`, default 1-year maximum lifetime, rotation with overlap.

**Slice 7 — review and docs**
- `docs/SECURITY_AUTH_ASVS.md` (OWASP ASVS 4.0.3 L2 mapping), RUNBOOK "Enterprise auth go-live (P42)", `.env.example`, `docs/TRUST_AND_SAFETY_FEATURES.md`.

## Tests
Backend: `test_p42_credentials.py`, `test_p42_network.py`, `test_p42_sessions.py`,
`test_p42_passkey_policy.py`, `test_p42_audit_keys.py`, `test_p42_saml.py`,
`test_p42_domains_scim.py`, plus the existing auth, 2FA, P25 and P41 suites. Migrations
0044–0049 up/down/up on embedded Postgres. Frontend: `P42Auth.test.tsx`,
`EnterpriseSsoCards.test.tsx`, typecheck.

## Not done (on purpose)
- External penetration test: recommended before the first large enterprise deal.
- IdP-initiated SAML, encrypted assertions, signed AuthnRequests, SAML single logout.
- SCIM bulk, sorting, ETags, and filters other than `userName eq` / `displayName eq`.
