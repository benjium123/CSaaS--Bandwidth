# Merge plan — `p41-kyc` (P41 trust & safety, P42 auth, P43 AI safety) into `main`

Fable, 2026-09-17. Status: **written, not started.** Owner of `main`: this session.
Owner of the branch: a Claude Code session in the Desktop app on the operator's other PC —
not reachable by cross-session message from here, so every "branch side" step below is
either relayed by the operator or done here on a local copy of the branch.

The branch is 16 commits, 176 files, +66,702/-12,878, 25 new backend test files, 8 new
migrations. It is a large, coherent piece of work. This plan is about landing it **without
breaking the running product or locking the operator out**, not about reviewing its design.

---

## 1. What actually clashes

Merge base is `1187d62` (P38). `main` has moved on since: P39 (SignalWire texting) and
**P41 messaging health** (migration `0044_messaging_health`, deployed 2026-09-16).

| # | Clash | Severity | Fix |
|---|---|---|---|
| 1 | **Two alembic heads.** `0044_account_security` and `0044_messaging_health` both have `down_revision = "0043_plan_allowances"`. After a merge, `alembic upgrade head` fails with multiple heads, and the branch's 8 migrations never apply. | **Blocker** | Repoint `0044_account_security.down_revision` to `"0044_messaging_health"`. One line, branch side. No change to the live DB, which is already at `0044_messaging_health`. |
| 2 | **Both phases are called "P41"** — messaging health here, account security there. `docs/ROADMAP.md` and `docs/RUNBOOK.md` conflict on exactly those rows. | Cosmetic, confusing | Rename the branch's phases to **P44/P45/P46** (or renumber mine). Decide before merging; renaming after release is worse. |
| 3 | Text conflicts: `docs/ROADMAP.md`, `docs/RUNBOOK.md`, `frontend/openapi.json`, `frontend/src/api/types.gen.ts`. | Low | Docs resolved by hand; the two generated files are regenerated after the merge, never hand-merged. |
| 4 | Auto-merged but semantically overlapping: `api/routes/platform.py`, `models/__init__.py`, `models/messaging.py`, `services/messaging.py`, `services/sweeper.py`. Both sides add sweeper ticks and platform routes. | Medium | Read each of the five by hand after the merge; run the P41-messaging-health tests specifically. |

**Not a clash:** the AI voice worker. It authenticates through its own `_require_worker`
guard on `/api/v1/agent/*`, not `get_current_user`, so `AUTH_BEARER_COMPAT=false` does not
touch it. Verified on the branch.

## 2. What changes for people using the product

These are defaults on the branch, not bugs. They are why this is a staged release.

| Setting | Branch default | Effect on the live org |
|---|---|---|
| `KYC_ENFORCED` | `true` | Business verification gates number ordering and telephony (`services/telephony_access.py`, `routes/numbers.py`, `routes/orgs.py`). The operator's own org is unverified today. |
| `REQUIRE_PASSKEY_FOR_PRIVILEGED` | `true`, 14-day grace | Owner/admin accounts must register a passkey. |
| mandatory 2FA (P41a) | on | Every account. |
| `AUTH_BEARER_COMPAT` | `false` | Sessions move to HttpOnly cookies + CSRF. Any script or integration holding a bearer token stops working. |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | 30 / 12 | Console sessions expire. |
| `WEBAUTHN_RP_ID` | empty | **Passkeys silently fail until set** to `csaas.sabinepropertygroup.net`. |
| `SECURITY_DATA_DIR` | `var/security` | KYC evidence. Needs a persistent path/volume or it is lost on redeploy. |
| new containers | `call-monitor` | A second always-on agent container (cpus 2, mem 2g) on a shared box. |
| new outbound calls | Stripe Identity, HIBP, Tor exit list, Cloudflare DoH, Companies House | New third-party dependencies and, for Stripe Identity, per-check cost. |
| new deps | `webauthn`, `pypdfium2`, `pillow`, `maxminddb`, `signxml` | Docker image rebuild. |
| nginx | new CSP + `Permissions-Policy` + rate-limit location for auth paths | Operator step; CSP can break the console if anything loads off-origin. |

## 3. Order of work

### Phase A — branch side (one line, then hand back)
A1. `0044_account_security.down_revision = "0044_messaging_health"`.
A2. Decide the renumbering (P44/P45/P46) and apply it in the branch's docs.
A3. Push. If the Desktop session cannot be reached, do A1–A2 here on a local branch
    `merge/p41-kyc` and tell that session what changed so it does not diverge.

### Phase B — merge, locally, nothing deployed
B1. `git checkout -b merge/p41-kyc main && git merge origin/p41-kyc`.
B2. Resolve the 4 text conflicts; regenerate `openapi.json` + `types.gen.ts` rather than
    merging them.
B3. Read the 5 auto-merged files by hand (§1 row 4).
B4. `alembic heads` → must print exactly one. `alembic upgrade head` on a scratch DB from a
    dump of production, then `downgrade` back one revision per new migration (all 8 define
    `downgrade`), then up again. A migration that cannot round-trip does not ship.
B5. Full backend suite per file with the chunked runner (`scratchpad/run_chunked.py`,
    600 s cap — `test_agent_tools.py` alone takes ~8.5 min). Frontend `vitest` + typecheck.
    Baseline to beat: 1829 passed, 1 known flake (D63).
B6. Specifically re-run P41 messaging health, P39 SignalWire, P37c allowances and the
    voice-plane tests: those are what `main` added after the merge base.

### Phase C — make it safe to switch on
C1. Set in `/opt/csaas/.env` **before** the first deploy:
    `KYC_ENFORCED=false`, `REQUIRE_PASSKEY_FOR_PRIVILEGED=false`, `AUTH_BEARER_COMPAT=true`,
    `WEBAUTHN_RP_ID=csaas.sabinepropertygroup.net`, `SECURITY_DATA_DIR` on a persistent path,
    SMTP settings (security alerts and verification emails are useless without them).
C2. Confirm whether mandatory 2FA has its own switch. If it does not, that is the one item
    that needs a code-level flag before deploy — an operator who cannot complete 2FA
    enrolment is locked out of their own console.
C3. Write the rollback: previous image tag + `alembic downgrade 0044_messaging_health`,
    tested in B4, plus the `.env` backup.

### Phase D — deploy, enforcement off
D1. Deploy in a quiet window. Migrations run; features exist; nothing is enforced.
D2. Operator smoke test, in this order: sign in, register a passkey voluntarily, order
    nothing, send one SMS, place one call (see §4), open the Platform page, confirm
    messaging health still renders.
D3. Watch `csaas-api-1` logs for auth errors for 24h.

### Phase E — switch on, one at a time
E1. `WEBAUTHN_RP_ID` proven → `REQUIRE_PASSKEY_FOR_PRIVILEGED=true` (14-day grace).
E2. 2FA enrolment completed by every real account → mandatory 2FA on.
E3. `AUTH_BEARER_COMPAT=false` only after confirming nothing but the console authenticates
    as a user (the agent worker does not — §1).
E4. Operator's own org verified through the KYC flow → `KYC_ENFORCED=true`. Verify first,
    enforce second, never the reverse.
E5. nginx CSP + auth rate-limit block, with a backup of the current conf and a 15-minute
    watch on the console afterwards.

## 4. Open question this plan does not answer

CSaaS currently **cannot place outbound calls**: its LiveKit outbound trunk was deleted
2026-09-17 (it ran on the CRM's Telnyx credentials), and `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`
points at a dead id on purpose. Restoring calling is **P37b** (per-org SIP connection and
trunk), not this merge. D2's "place one call" is therefore expected to fail with a clear
error until P37b lands — that failure is the correct behaviour, and confirming the error is
clean (not a 500) is the actual test.

## 5. Effort

Phase A minutes. B half a day, mostly test time. C–E spread over about a week of the
operator's evenings, because each switch wants a day of watching. The risk is concentrated
entirely in E, and every step of E is one `.env` line and a container restart to reverse.
