# Authentication review — OWASP ASVS 4.0.3, Level 2

Scope: V2 (authentication), V3 (session management), V4 (access control) and the auth-related
parts of V8, V13 and V14. Reviewed 2026-09-17 against branch `p41-kyc` after P42.
Status: **Met**, **Partial** (works, with a stated limit) or **Gap** (not done).
This is a self-review, not a certification. An external penetration test is still
recommended before the first large enterprise customer.

## V2 Authentication
| ASVS | Requirement (short) | Status | Where |
|---|---|---|---|
| 2.1.1 / 2.1.2 | Passwords 12+ chars, long passwords allowed | Met | `services/password_policy.py` (min 12, no low maximum) |
| 2.1.7 | Check against breached passwords | Met | HIBP k-anonymity range API, padded; fails open with a log line |
| 2.1.9 | No composition rules | Met | length + breach + not-the-email only |
| 2.1.12 | User can see a masked password | Met | browser default; no paste blocking |
| 2.2.1 | Anti-automation (rate limit, lockout) | Met | Redis sliding window (`rate_limit.py`), nginx `limit_req`, progressive lockout (`services/lockout.py`) |
| 2.2.2 | No weak authenticators (SMS/email OTP) as primary 2FA | Met | TOTP and passkeys only; never SMS |
| 2.2.3 | Notify on credential changes | Met | `account_security.notify_now`: password, factors, recovery, lockout |
| 2.2.4 | Phishing-resistant MFA available | Met | WebAuthn passkeys; required for privileged roles (`passkey_policy.py`) |
| 2.3.1 | Initial/temporary secrets random and short-lived | Met | invite and reset tokens 32 random bytes, hashed, single use; reset 30 min |
| 2.4.1 / 2.4.4 | Passwords hashed with an approved KDF | Met | argon2id (`auth/security.py`), rehashed on login when parameters change |
| 2.5.1 / 2.5.2 | Recovery secrets random, no password hints/questions | Met | reset tokens; no security questions |
| 2.5.3 | Recovery doesn't reveal the current password | Met | |
| 2.5.4 | No shared/default accounts | Met | named platform operators (P41) |
| 2.5.6 | Forgotten password flow is secure | Met | always 202 (no account enumeration), sessions revoked, 2FA still required after reset |
| 2.5.7 | Losing MFA doesn't bypass it | Met | recovery codes, ID+selfie matching the verified person, admin/operator reset — all audited, cool-down on sensitive actions |
| 2.7.x | Out-of-band authenticators | N/A | not used |
| 2.8.1–2.8.4 | TOTP time window, single use per window | Met | ±1 step; last used step stored, reuse refused (`routes/twofa.py`) |
| 2.9.x | Cryptographic authenticators | Met | WebAuthn signature and sign-count checks (`services/passkeys.py`), single-use challenges |
| 2.10.1–2.10.4 | Service credentials not hard-coded, stored protected | Met | API keys and SCIM tokens stored as SHA-256; IdP secrets Fernet-encrypted |

## V3 Session management
| ASVS | Requirement (short) | Status | Where |
|---|---|---|---|
| 3.1.1 | No session tokens in URLs | Met | cookie only; softphone WebSocket query token only under `AUTH_BEARER_COMPAT` |
| 3.2.1 | New token on authentication | Met | `login_flow.complete_login` issues a fresh session; rotated on step-up and password change |
| 3.2.2 / 3.2.3 | 128+ bits entropy, stored safely in the browser | Met | 32-byte secret, HttpOnly cookie, SHA-256 at rest |
| 3.3.1 | Logout invalidates server-side | Met | `POST /auth/logout` revokes the row |
| 3.3.2 | Idle and absolute timeouts | Met | 30 min / 12 h, stricter per workspace |
| 3.3.3 | Option to end other sessions after credential change | Met | automatic on password change and 2FA reset |
| 3.3.4 | Users can see and end active sessions | Met | Settings > Security > Sessions |
| 3.4.1–3.4.3 | Secure, HttpOnly, SameSite | Met | `__Host-` prefix in production, SameSite=Lax |
| 3.4.4 / 3.4.5 | `__Host-` prefix, path scoped | Met | `session_tokens.session_cookie_name` |
| 3.5.2 | Stateless tokens not used for sessions | Partial | bearer JWTs accepted only while `AUTH_BEARER_COMPAT=true` (cut-over) |
| 3.5.3 | Signed tokens protected against tampering | Met | JWT HS256 with secret; SAML/OIDC signatures verified |
| 3.7.1 | Re-authentication before sensitive actions | Met | `recent_2fa` / `recent_selfie` step-up (`auth/deps.py::check_step_up`) |

## V4 Access control
| ASVS | Requirement (short) | Status | Where |
|---|---|---|---|
| 4.1.1 / 4.1.3 | Enforced server-side, least privilege | Met | `require_permission`, role permissions, tenant guard (`db/base.py`) |
| 4.1.5 | Fail securely | Met | missing tenant context raises; SSO state store fails closed |
| 4.2.1 | IDOR protection | Met | every query org-scoped; unscoped queries marked `JUSTIFIED allow_unscoped` |
| 4.2.2 | CSRF protection | Met | double-submit HMAC token for cookie requests; SAML ACS bound by single-use RelayState |
| 4.3.1 | MFA for administrative interfaces | Met | operators need a second factor + passkey policy; `/ops` actions need fresh 2FA |
| 4.3.2 | No directory browsing / metadata leaks | Met | |

## Federation (SSO) — ASVS V2/V3 + SAML/OIDC practice
| Check | Status | Where |
|---|---|---|
| Domain ownership proven before SSO signs anyone in | Met | `services/sso_provisioning.py`, `org_domains` DNS TXT |
| OIDC: state, nonce, issuer, audience, signature, expiry | Met | `services/oidc.py` |
| SAML: signature required, XSW-safe (read from verified element only), no DTD/XXE | Met | `services/saml.py`, `tests/test_p42_saml.py` |
| SAML: Audience, Recipient, NotBefore/NotOnOrAfter, InResponseTo, replay | Met | same |
| SAML: SHA-1 refused, encrypted assertions | Met / not supported | signxml defaults; encrypted refused |
| JIT provisioning never grants owner | Met | `_role_for_new_member` |
| SCIM: hashed tokens, verified domains only, deprovision ends sessions, owners protected | Met | `routes/scim.py`, `tests/test_p42_domains_scim.py` |

## V8 / V13 / V14 (auth-related)
| ASVS | Requirement (short) | Status | Where |
|---|---|---|---|
| 8.3.x | Sensitive data not cached | Met | `Cache-Control: no-store` on auth, KYC, ops |
| 7.1.x | Auth events logged without secrets | Met | `login_events`, `account_audit_log`, org `audit_log` |
| 13.2.x | API keys: scope, expiry, network limits | Met | scopes, 1-year maximum, CIDR allowlist, rotation overlap |
| 14.4.x | Security headers (CSP, HSTS, nosniff, frame) | Met | `main.py` middleware, `deploy/nginx-csaas.conf` |
| 14.5.3 | Trusted client IP only from known proxies | Met | `app/net.py::client_ip` with `TRUSTED_PROXY_COUNT` |

## Known gaps and follow-ups
1. Bearer JWT compatibility must be switched off after cut-over (`AUTH_BEARER_COMPAT=false`).
2. HIBP check fails open when the service is down (deliberate availability trade-off).
3. No SAML single logout; IdP sessions are ended by our idle/absolute timeouts and SCIM deprovisioning.
4. External penetration test not yet done.
