# P14 Security Review

**Reviewer:** Fable (Tier-1, phase-14-plan DR-10) · **Date:** 2026-08-29 ·
**Scope:** authn/z surfaces, tenant isolation, secret handling, SSRF/egress, abuse
surfaces, deploy path — the codebase as of P13 landing (`d497cfb`) plus the P14
working tree. Evidence: direct code reads, the adversarial Opus review probes from
P11–P13 (cross-org IDOR probes on all P13 platform lookups, hash-only key storage,
constant-time comparisons — all verified there), and live probes against the deployed
box where noted.

## Findings

| # | Sev | Finding | Disposition |
|---|---|---|---|
| S1 | **MEDIUM** | **No rate limiting or lockout on `/auth/login` or 2FA verification.** argon2id makes each password guess expensive server-side but nothing bounds attempts; the sharper edge is unthrottled 6-digit TOTP verification (`/twofa`), which is brute-forceable in bounded time without a limiter. | **Fix scheduled** — in-process per-identifier limiter on login + TOTP verify (OPEN_ISSUES D26; small, no new deps). Until then the exposure is bounded by TOTP secret quality and argon2 cost. |
| S2 | LOW | **Softphone events WS carries the JWT in a query param** (`/api/v1/events/ws?token=…`) — browsers can't set WS headers. Verified it uses the SAME decoder + membership check as HTTP; the residual risk is token exposure in proxy/access logs. TTL ≤ jwt_expire_hours. | Accepted with note (OPEN_ISSUES D27): a short-lived WS-scoped ticket endpoint is the clean fix later. Nginx on the box does not log query strings for this vhost by default config — operator should keep it that way (runbook note). |
| S3 | LOW | **Outbound-webhook SSRF guard has a DNS-rebinding TOCTOU**: the private/loopback check runs on our resolution; httpx re-resolves at send. https-only, no redirects, re-checked per delivery. | Accepted and DOCUMENTED in the module (D21). Pinned-IP transport closes it if webhooks ever carry sensitive payloads beyond the org's own event data. |
| S4 | LOW | **Carrier breaker + health state is in-process** (P3b DR-3 by design): a multi-worker deployment would not share failover state, and `/status` on one worker may disagree with another. Current deploy is single-process. | Accepted; runbook states the single-process assumption. Revisit with any gunicorn multi-worker change. |
| S5 | INFO | **Whisper is honestly disabled** (FeatureUnavailableError): verified live that no server-side subscription-permission API exists; a fake-enforced whisper (caller hears the supervisor) was rejected in review (P12 B7). | Correct posture. D15 tracks the client-side implementation. |
| S6 | INFO | Production settings validation is real: open registration refused, loopback carrier refused, `PUBLIC_BASE_URL` must be https, Fernet key validated, Bandwidth webhook credentials required when live (voice verify fail-closed). CORS is explicit-origin allowlist. | No action. |
| S7 | INFO | API keys: `csk_` format, SHA-256 hash-only storage, constant-time compare, prefix-indexed lookup (existence-of-prefix timing is the only oracle — acceptable), wildcard scope rejected at creation AND stripped defensively at auth, org-bound, 401/403 never merged. Webhook secrets Fernet-encrypted, shown once. Neither ever serialized back out (Opus-probed). | No action. |
| S8 | INFO | Tenant isolation is structural (D9 session hooks: unscoped queries raise; cross-tenant writes refused) and was re-probed adversarially this cycle: P13 platform lookups (5 endpoints) with a second org's credentials → 403/404; the P12 webhook path resolves org before any flow code and a crafted digit event cannot reach another org's call. The one systemic hazard found this cycle — multi-org sweeper loops autoflushing under the wrong org context — was PROVEN and fixed (P12 B1/B2), with `services/media.py` carrying the same latent pattern (D19, scheduled). | D19 remains open. |
| S9 | INFO | Machine seams: all 7 AI-worker routes gate on the worker JWT (HS256 with the LiveKit secret, `iss`/`sub`/`exp` enforced); a user bearer token fails signature there. Inbound carrier webhooks: Bandwidth Basic (constant-time, fail-closed when creds missing), Telnyx Ed25519 with 300s replay window. | No action. |
| S10 | INFO | Secrets in logs: no `get_secret_value()` reaches a log call; `provider_statuses()` reports variable NAMES only; audit `detail` carries no secrets (reviewed in P13). | No action. |
| S11 | LOW | **Uploads**: list import capped (10MB/100k rows, extension-checked before read — P11 B5); media uploads capped at 3.75MB. No global request-body cap at the app layer — nginx's `client_max_body_size` is the outer bound. | Runbook documents the nginx expectation. |

## Deploy path

`deploy.sh` ships `git archive HEAD` only (no working-tree leakage), never writes
`.env`, aborts on conflict, and health-checks before declaring success. Backups
(`backup.sh`) are root-read-only (`chmod 600`) and read NO password at all
(`docker exec` + the postgres local socket). The restore drill (post-review fix B1)
reads NOTHING from `.env` either: the throwaway postgres gets a random one-shot
password, its own bridge network (the live `db` hostname does not resolve there), and
no published host port — command lines and `docker inspect` on the shared box carry no
live secret.

## Verdict

No critical or high findings. One medium (S1) with a scheduled fix; the rest are
accepted-and-documented low/informational items tracked in OPEN_ISSUES. The two
strongest properties of this codebase — structural tenant isolation and
fail-closed/honest seams (no fake whisper, no fake transcripts, no fake scores) —
held up under adversarial review at every phase this cycle.
