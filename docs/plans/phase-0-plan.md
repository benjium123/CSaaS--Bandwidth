# Phase 0 — Foundation

> Refined by Fable (Tier-1) 2026-08-26. Workstreams: WS-0 (Foundation), WS-10 (DevOps).
> Blocker risk R1 tracked below. Track R (brand registration) runs in parallel — it is
> paperwork, not code, and is NOT part of this plan's implementation scope.

## Goal

Ship a running, deployable FastAPI skeleton with real multi-tenancy and RBAC: orgs, users,
memberships, roles, permission checks — with `org_id` isolation enforced *mechanically* at
the ORM/session layer so it cannot be forgotten per-query. Boot validates `.env` via a
Pydantic settings object that logs exactly which providers are disabled and why, without
ever logging a secret. Structured JSON logging + an error taxonomy. Alembic migrations.
CI that runs the suite on SQLite (locally reproducible) *and* on real Postgres 16.
Deployed to the VPS with `/healthz` green. Gate: **`/healthz` green locally and on the VPS,
and a test — not an inspection — proves org B cannot read org A's rows.**

---

## Decision Record (the refinement pass — these are settled for P0)

### DR-1: Test strategy without local Docker/Postgres → **dual-backend: SQLite locally, Postgres in CI, with three mechanical guards**

Local machine has Python 3.10, no Docker, no psql. Tests MUST pass on this machine today.

- **Local default:** pytest runs against **in-memory SQLite via `aiosqlite`**
  (`sqlite+aiosqlite://`). Schema is built with `Base.metadata.create_all` per test session.
- **CI truth:** a second CI job runs against a **real `postgres:16` service container**:
  `alembic upgrade head` first (this is what actually tests the migrations — locally we
  never run Alembic against SQLite), then the **entire** pytest suite with
  `DATABASE_URL` pointed at the container.
- **Guard 1 — portable types only.** All models use `app/db/types.py`:
  `GUID` (TypeDecorator: native `UUID` on PG, `CHAR(36)` on SQLite) and
  `PortableJSON` (`sa.JSON().with_variant(JSONB, "postgresql")`). Timestamps are
  `sa.DateTime(timezone=True)` and always tz-aware in Python.
- **Guard 2 — dialect-import ban, enforced by CI.** `sqlalchemy.dialects.postgresql` may be
  imported ONLY in `app/db/types.py` and `migrations/`. Enforced with Ruff
  `flake8-tidy-imports` banned-api config in `pyproject.toml` — a violation fails lint, so
  Postgres-only SQL can't hide in app code where SQLite tests wouldn't catch it. Raw
  `sa.text(...)` in `app/` is likewise banned (allowed in `migrations/` and the healthz DB
  ping helper only, via per-file ignore).
- **Guard 3 — marker convention.** A test that genuinely needs Postgres semantics is marked
  `@pytest.mark.pg_only`; conftest skips it when the backend is SQLite, CI runs it on the
  PG job. `pyproject.toml` registers the marker with `--strict-markers` so typos fail.
- The **merge gate is the Postgres CI job**, not the SQLite one. SQLite is a fast local
  proxy, never the authority.

### DR-2: Python version policy → **P0 code targets 3.10; runtime containers pin 3.12; CI matrix {3.10, 3.12}**

- Source must run on **3.10** (local reality). No 3.11+-only syntax: no `tomllib`, no
  `except*`/ExceptionGroup, no `typing.Self`, no `StrEnum`, no `asyncio.timeout()` (use
  `asyncio.wait_for`). `X | Y` unions and `match` are fine (3.10). Enforced by
  `requires-python = ">=3.10"`, Ruff `target-version = "py310"`, and the 3.10 CI job.
- The **Dockerfile pins `python:3.12-slim`** — the VPS runs 3.12 from day one, so the
  eventual local upgrade changes nothing in prod.
- **Tracked risk (add to docs/PROGRESS.md):** *"Local dev machine must be on Python 3.12
  (or a 3.12 venv via a real 3.12 install) before P7 starts — Pipecat requires 3.11/3.12.
  Deadline: P6 sign-off. Owner: user."* Do not slip this to P7 day 1.

### DR-3: Multi-tenancy + RBAC schema and enforcement → see Implementation Notes §2–3

Summary: 5 tables (`orgs`, `users`, `roles`, `org_memberships`, plus Alembic's version
table). Users are global (one login, N orgs); the **org context comes per-request from an
`X-Org-Id` header validated against membership** — JWTs carry identity only, so switching
orgs never mints a new token. Isolation is enforced at the **session layer** with a
SQLAlchemy `do_orm_execute` hook + `with_loader_criteria`: every ORM SELECT against a
tenant-scoped model gets `org_id = :ctx` injected automatically, and a query with **no org
context raises `MissingTenantContextError`** instead of silently returning everything.
The repository layer rides on top for ergonomics; the hook is the safety net that makes
"forgot the filter" impossible rather than unlikely.

### DR-4: Settings validation + secret redaction → see Implementation Notes §4

All secret-bearing fields are `SecretStr`. Boot logs one status line per provider naming
the **missing variable names**, never values. A test plants a sentinel secret value and
asserts it appears in **zero** log output.

### DR-5: Scaffold → **do NOT fork the template; use its layout as reference, generate a leaner app**

Two sentences of justification: the template's actual deliverables (JWT plumbing, a
superuser/user binary, its own React frontend, Docker-first dev loop, copier machinery) are
exactly the parts we must rip out or can't run locally — no Docker, and its auth model is
the thing D3 says we replace. Writing ~20 focused files gets a green 3.10 test suite today,
while a fork starts life red and carries dead weight we'd be deleting through P2.

### DR-6: Out of scope for P0 → see the explicit list at the end of Implementation Notes

Notably: **2FA/TOTP is deliberately moved from P0 to P2** (SPEC deviation, recorded here) —
without a console there is no enrollment surface, so a P0 2FA would be untestable
end-to-end and pure shelf-ware. The schema leaves room (`users.totp_secret` is NOT added
now; it's a P2 migration).

---

## Pre-dependencies

- Python 3.10.10 (present). Create venv: `python -m venv .venv` in `backend/`.
- Packages (pinned in `backend/pyproject.toml`, installed with `pip install -e .[dev]`):
  - runtime: `fastapi`, `uvicorn[standard]`, `sqlalchemy>=2.0`, `alembic`, `asyncpg`,
    `aiosqlite`, `pydantic>=2`, `pydantic-settings`, `structlog`, `PyJWT`,
    `argon2-cffi`, `python-multipart`
  - dev: `pytest`, `pytest-asyncio`, `httpx`, `ruff`
  - **No other dependencies without Fable approval.** Notably NOT passlib (unmaintained),
    NOT python-jose (CVE history) — PyJWT + argon2-cffi directly.
- `.env` locally already exists; tests never read it (conftest builds settings explicitly).
- **R1 (user-owned, not code):** open/confirm the Bandwidth account path to production
  ("Bandwidth Build" self-serve → confirm it scales past trial without a sales contract).
  P0 code does not depend on it, but P0 is not *signed off* until R1 has a definitive
  answer recorded in `docs/PROGRESS.md` and `docs/BRAND_REGISTRATION.md`.

## Allowed Files (implementer may create/read/write — nothing else)

```
backend/pyproject.toml
backend/alembic.ini
backend/app/__init__.py
backend/app/main.py
backend/app/config.py
backend/app/logging.py
backend/app/errors.py
backend/app/db/__init__.py
backend/app/db/base.py
backend/app/db/types.py
backend/app/db/session.py
backend/app/models/__init__.py
backend/app/models/org.py
backend/app/models/user.py
backend/app/models/rbac.py
backend/app/repositories/__init__.py
backend/app/repositories/base.py
backend/app/repositories/orgs.py
backend/app/repositories/users.py
backend/app/auth/__init__.py
backend/app/auth/security.py
backend/app/auth/deps.py
backend/app/api/__init__.py
backend/app/api/routes/__init__.py
backend/app/api/routes/health.py
backend/app/api/routes/auth.py
backend/app/api/routes/orgs.py
backend/migrations/env.py
backend/migrations/script.py.mako
backend/migrations/versions/0001_foundation.py
backend/tests/__init__.py
backend/tests/conftest.py
backend/tests/test_healthz.py
backend/tests/test_settings.py
backend/tests/test_auth.py
backend/tests/test_tenancy.py
backend/tests/test_rbac.py
.github/workflows/ci.yml
deploy/Dockerfile
deploy/docker-compose.prod.yml
deploy/deploy.sh
docs/PROGRESS.md            (status updates only)
README.md                   (add local run/test instructions section only)
```

## Forbidden (implementer must never touch)

- `.env` (local or on the VPS) and any secrets
- `docs/` other than `docs/PROGRESS.md` (ARCHITECTURE, PHASES, SPEC, WORKSTREAMS,
  BRAND_REGISTRATION are Fable-owned)
- **Anything on the VPS other than `/opt/csaas`.** The VPS runs live production services
  (Postgres :5433 and :5544 among them). No global package installs, no nginx main-config
  edits, no systemd changes outside a `csaas`-prefixed unit, no port publications that
  collide (deploy.sh pre-flight-checks its ports before starting anything).
- `.env.example` — the settings object must match the existing file; if a mismatch is
  found, report it, don't edit.
- Any file not in the Allowed list. Max ~35 new files as listed; no extra scaffolding,
  no frontend, no `providers/` directory yet.

## Implementation Notes

### 1. App skeleton

`app/main.py` builds the FastAPI app via a factory `create_app(settings)`. Lifespan:
init structlog → validate settings (fail fast) → log provider status report → create
engine → yield → dispose engine. Routers: `/healthz`, `/api/v1/auth/*`, `/api/v1/orgs/*`.
CORS from `settings.cors_origins`. Version string surfaced from `pyproject.toml` metadata
(read via `importlib.metadata`, NOT `tomllib` — 3.10).

`/healthz` returns `{"status":"ok","env":...,"version":...,"db":"ok"}` with a `SELECT 1`
ping bounded by `asyncio.wait_for(..., 2.0)`; DB down → 503 with `"db":"unreachable"`.
No auth on healthz.

### 2. Schema (migration `0001_foundation`)

All PKs are `GUID` (app-generated `uuid4` — portable, no DB extension needed). All tables
have `created_at`/`updated_at` `DateTime(timezone=True)` server-defaulted/onupdate.

- **orgs**: `id`, `name` (str 255), `slug` (str 63, unique, lowercase), `is_active` bool.
- **users** (global, NOT org-scoped): `id`, `email` (str 320, unique, stored lowercased),
  `hashed_password` (argon2id), `full_name`, `is_active`. **No superuser flag** — platform
  admin is a P13/P14 concern; the binary is exactly what we refused to inherit.
- **roles** (org-scoped): `id`, `org_id` FK→orgs CASCADE, `name` (str 63),
  `permissions` `PortableJSON` (list of permission keys), `is_system` bool,
  unique `(org_id, name)`. Org creation seeds three system roles from a code-level
  catalog: **owner** (`["*"]`), **admin** (all except `org:delete`, `org:billing`),
  **agent** (`["inbox:read","inbox:send","contacts:read","contacts:write"]`).
  Permission keys are `resource:action` strings; the catalog constant
  `PERMISSIONS` lives in `app/models/rbac.py` and unknown keys are rejected on role write.
  (Permissions-as-JSON on the role row, not a join table: P0 has no custom-role editor,
  and the catalog is code-defined; normalize later if/when custom roles ship.)
- **org_memberships** (org-scoped): `id`, `org_id` FK CASCADE, `user_id` FK CASCADE,
  `role_id` FK→roles RESTRICT, unique `(org_id, user_id)`.

`TenantScoped` is a mixin declaring `org_id` (GUID, FK, `nullable=False`, **indexed**).
`Role` and `OrgMembership` use it. Every future tenant table uses it — that's the hook's
contract.

### 3. Tenancy enforcement — the mechanism (this is the heart of P0)

Two layers; the second makes the first unforgettable:

1. **Repository layer** (`repositories/base.py`): `TenantRepository` is constructed with
   `(session, org_id)`; its `get/list/create/delete` helpers apply/assign `org_id`
   automatically. Route code never touches a bare session for tenant models.
2. **Session-level hard guard** (`db/base.py`): the session is created with
   `info={"org_id": <ctx or None>}`. A `do_orm_execute` event listener on the Session
   class: for every ORM SELECT/UPDATE/DELETE whose entities include a `TenantScoped`
   subclass —
   - if `info["org_id"]` is set → inject `with_loader_criteria(TenantScoped,
     lambda cls: cls.org_id == org_id, include_aliases=True)`;
   - if it is None and the execution is not explicitly flagged
     `execution_options(allow_unscoped=True)` → **raise `MissingTenantContextError`**.
   `allow_unscoped` is used exactly twice in P0: auth's user lookup path (users aren't
   tenant-scoped anyway, so in practice only membership-listing at login) and the org
   bootstrap that creates the first membership. Each use carries a comment justifying it.
3. **Creates are guarded too**: `TenantRepository.create` stamps `org_id`; additionally a
   `before_flush` listener rejects a new `TenantScoped` instance whose `org_id` differs
   from the session's context (or is unset) unless `allow_unscoped` was set — blocking
   cross-tenant *writes*, which `with_loader_criteria` alone does not cover.

**Request wiring** (`auth/deps.py`):
- `get_current_user`: decode Bearer JWT (HS256, `sub`=user id, `exp` 24 h) → load user →
  401 on any failure, 403 if inactive.
- `get_current_org`: read `X-Org-Id` header (400 if absent/malformed on org-scoped routes)
  → verify an `org_memberships` row for (user, org) → 403 if none → set the session's
  `info["org_id"]` → return `(org, membership, role)`.
- `require_permission("orgs:read")` etc.: dependency factory; checks the resolved role's
  permission list (`"*"` short-circuits) → 403 with error code `permission_denied`.

### 4. Settings (`app/config.py`)

`Settings(BaseSettings)` reading `.env` (`extra="ignore"` so later-phase vars don't break
boot). Field names mirror `.env.example` exactly. Every credential-bearing field
(`*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN`, `*_DSN`, JWT_SECRET, SESSION_SECRET,
CREDENTIAL_ENCRYPTION_KEY) is **`SecretStr`** — that is the redaction rule: secrets are
un-loggable by type; `repr(settings)` and structlog serialization show `**********`.

Cross-field validation (fail fast at boot, all failures aggregated into one error):
- `JWT_SECRET`, `SESSION_SECRET` required always; `CREDENTIAL_ENCRYPTION_KEY` required
  and must parse as a Fernet key **when `APP_ENV=production`** (warn-if-missing in dev).
- `APP_ENV=production` additionally requires: `PUBLIC_BASE_URL` https, non-example;
  `DATABASE_URL` not pointing at localhost defaults with default creds.

**Provider status report**: `settings.provider_statuses()` returns
`[ProviderStatus(name, enabled: bool, reason: str | None, missing: list[str])]` for
bandwidth, telnyx, each LLM/STT/TTS vendor, S3, Redis, SMTP, Sentry. Logic: the block's
`*_ENABLED` flag AND its required fields. Boot logs one line each, e.g.
`provider=bandwidth enabled=false reason="BANDWIDTH_ENABLED=true but missing: BANDWIDTH_ACCOUNT_ID, BANDWIDTH_API_PASSWORD"`
— **variable names only, never values.** P0 only *reports*; nothing consumes these
providers yet.

### 5. Auth (`auth/security.py`, `api/routes/auth.py`)

- argon2id via `argon2-cffi` (`PasswordHasher()` defaults), `verify` + transparent
  `check_needs_rehash` on login.
- Endpoints: `POST /api/v1/auth/register` (email+password+full_name → creates user; org
  membership comes separately), `POST /api/v1/auth/login` (OAuth2 password form → JWT;
  same 401 + same response time shape for unknown-email vs bad-password),
  `GET /api/v1/auth/me` (user + list of `{org_id, org_name, role_name}` memberships).
- Org endpoints: `POST /api/v1/orgs` (auth'd user → creates org, seeds 3 system roles,
  creates owner membership for creator), `GET /api/v1/orgs/current` (org-scoped, needs
  `orgs:read`), `GET /api/v1/orgs/current/roles` (org-scoped — this is the resource the
  tenancy tests read across tenants), `GET /api/v1/orgs/current/members`
  (needs `members:read`; admin+owner have it, agent does not — gives RBAC a real deny).

### 6. Errors + logging

- `app/errors.py`: `CsaasError(code, message, http_status)` hierarchy —
  `not_found` 404, `permission_denied` 403, `unauthenticated` 401,
  `missing_tenant_context` 500 (this one means a *programming bug*; log at ERROR),
  `validation_failed` 422, `conflict` 409. One exception handler renders
  `{"error": {"code", "message", "request_id"}}`. Unknown exceptions → 500
  `internal_error`, full traceback to logs only, never to the response body.
- `app/logging.py`: structlog, JSON in production, pretty console in dev. Middleware binds
  `request_id` (uuid4, echoed as `X-Request-Id`), method, path, org_id, user_id;
  access log line includes status + duration ms.

### 7. CI (`.github/workflows/ci.yml`)

- **job `lint`**: `ruff check` + `ruff format --check` (includes the dialect-import ban).
- **job `test-sqlite`**: matrix `python: [ "3.10", "3.12" ]` → `pytest -q`
  (pg_only auto-skipped). Proves 3.10 compat and 3.12 forward-compat on every push.
- **job `test-postgres`**: python 3.12 + `postgres:16` service container →
  `alembic upgrade head` → `pytest -q` with `TEST_DATABASE_URL` set to the container
  (whole suite including `pg_only`). **This job is the merge gate.**

### 8. Deploy (`deploy/`)

- `Dockerfile`: `python:3.12-slim`, non-root user, `pip install .`, CMD uvicorn.
- `docker-compose.prod.yml`: services `api` (published **127.0.0.1:8080** only),
  `db` (postgres:16, **no published port** — internal network only, volume
  `csaas_pgdata`), `redis` (no published port, present for parity; unused by P0 code).
  Compose project name `csaas`. Nothing binds 5433/5544 or any public interface.
- `deploy.sh` (run from dev machine, Git Bash): pre-flight `ssh root@144.126.152.175`
  checks — docker present (abort with instructions if not; do NOT auto-install on a
  production box), `/opt/csaas` created, port 8080 free-or-ours — then
  `git archive | ssh tar -x` into `/opt/csaas`, copy nothing over an existing `.env`
  (first deploy: operator creates `/opt/csaas/.env` by hand from `.env.example`),
  `docker compose -f deploy/docker-compose.prod.yml up -d --build`,
  `docker compose exec api alembic upgrade head`, then curl `127.0.0.1:8080/healthz`
  and print the JSON. Idempotent; re-running is safe. No nginx exposure in P0 —
  public routing is a later concern; the VPS gate is checked via ssh+curl.

### 9. Explicitly NOT in P0 (scope fence — reject any of these in review)

- No frontend of any kind (P2). No React, no Vite, no static files.
- No carrier code, no `providers/`, no webhook endpoints, no Bandwidth/Telnyx SDK calls
  (P1). R1 is a *paperwork* checklist item.
- No compliance module, no consent ledger (P3).
- No Redis usage in code, no S3 client, no MinIO, no object-store code — settings
  validation covers their config blocks, nothing consumes them (P1/P3).
- No 2FA/TOTP (moved to P2 — DR-6), no email sending, no invite flow, no password reset
  (P2, needs SMTP + console), no API keys (P13), no audit log table (P13).
- No user-facing org switching UI, no team management beyond the three endpoints listed.
- No credential-encryption *usage* — the Fernet key is validated, `app/auth/security.py`
  exposes `encrypt_credential/decrypt_credential`, but no table stores credentials yet (P1).
- No local Docker workflow, no devcontainer — local dev is venv + SQLite by design.
- No load testing, no rate limiting, no OpenTelemetry wiring (flag exists, ignored).

## Test Spec

All local commands run from `backend/` in the venv, on this Windows machine, today:

```
python -m pytest -q          # SQLite backend, pg_only skipped — must be green
python -m ruff check .       # includes the postgres-dialect import ban — must be clean
```

Unit tests:
- [ ] `test_settings.py::test_boot_fails_without_jwt_secret` → constructing Settings with
      `JWT_SECRET=""` raises, and the aggregated error names `JWT_SECRET`.
- [ ] `test_settings.py::test_provider_disabled_reason_names_missing_vars` → Settings with
      `BANDWIDTH_ENABLED=true` and empty account id → status for bandwidth has
      `enabled=False` and `"BANDWIDTH_ACCOUNT_ID" in missing`.
- [ ] `test_settings.py::test_secrets_never_logged` → set
      `BANDWIDTH_API_PASSWORD="sentinel-hunter2-XYZZY"`, boot the app with a capturing
      structlog processor, run the provider report → assert `"sentinel-hunter2-XYZZY"`
      appears in ZERO captured log lines AND `repr(settings)` AND
      `str(settings.bandwidth_api_password)` == `"**********"`.
- [ ] `test_auth.py::test_register_login_me` → register → login → `/auth/me` 200 with
      email; wrong password → 401 with `error.code == "unauthenticated"`.
- [ ] `test_auth.py::test_password_is_argon2` → stored hash starts `$argon2id$`.
- [ ] `test_tenancy.py::test_unscoped_query_raises` → open a session with
      `info["org_id"]=None`, `select(Role)` → `MissingTenantContextError`. Same session
      with `execution_options(allow_unscoped=True)` → succeeds.
- [ ] `test_tenancy.py::test_cross_tenant_write_rejected` → session scoped to org A,
      flush a Role with `org_id = org_b.id` → raises.
- [ ] `test_rbac.py::test_unknown_permission_key_rejected` → creating/seeding a role with
      permission `"bogus:nope"` raises `validation_failed`.

Integration tests (httpx `AsyncClient` against the app factory, SQLite):
- [ ] `test_healthz.py::test_healthz_green` → `GET /healthz` → 200, `status=="ok"`,
      `db=="ok"`, response carries `X-Request-Id`.
- [ ] `test_tenancy.py::test_org_b_cannot_read_org_a_rows` — **THE GATE TEST**:
      user1 creates org A; user2 creates org B; as user2 with `X-Org-Id: <A>` →
      403 (not a member — membership check fires before any query); as user2 with
      `X-Org-Id: <B>` → `GET /orgs/current/roles` returns ONLY roles whose ids were
      seeded for B — assert zero intersection with A's role ids; and a direct
      `TenantRepository(session, org_b.id).get(Role, a_role_id)` returns `None`.
- [ ] `test_tenancy.py::test_missing_org_header_is_400` → org-scoped route without
      `X-Org-Id` → 400, never a 500 and never data.
- [ ] `test_rbac.py::test_agent_denied_members_read` → member with `agent` role →
      `GET /orgs/current/members` → 403 `permission_denied`; same call as `admin` → 200.
- [ ] `test_rbac.py::test_owner_wildcard` → owner passes every `require_permission` in
      the app's route table (parametrized over the three org endpoints).
- [ ] `test_auth.py::test_forged_and_expired_jwt_rejected` → token signed with a wrong
      key → 401; token with `exp` in the past → 401.
- [ ] (`pg_only`) `test_tenancy.py::test_isolation_on_postgres` → re-run the gate test on
      the real PG backend, proving `with_loader_criteria` + GUID/JSON variants behave
      identically there.

Manual verification:
- [ ] `uvicorn app.main:app` locally (SQLite file DB for manual runs is acceptable via
      `DATABASE_URL=sqlite+aiosqlite:///./dev.db`) → open `http://localhost:8080/healthz`.
- [ ] Blank out `BANDWIDTH_ACCOUNT_ID` in local `.env`, boot, and eyeball the provider
      report: one line per provider, names of missing vars, no secret values anywhere.
- [ ] After deploy: `ssh root@144.126.152.175 "curl -s 127.0.0.1:8080/healthz"` → green,
      and `docker ps` on the box shows only `csaas_*` containers were added.
- [ ] CI: all three jobs green on the PR — **`test-postgres` is the merge gate.**
- [ ] R1 recorded: `docs/PROGRESS.md` states the confirmed Bandwidth account path
      (or the blocker escalation) before P0 sign-off.

Pass criteria: ALL unit + integration tests green locally on SQLite/3.10 AND on CI's
Postgres job. Commit only after pass criteria met: `feat(phase-0): foundation — tenancy,
RBAC, settings validation, CI, VPS skeleton deploy`.

## Deploy

**yes** — skeleton to the VPS per Implementation Notes §8. Constraints restated because
the box is production for other businesses: everything under `/opt/csaas`, compose project
`csaas`, api bound to 127.0.0.1:8080 only, db/redis unpublished, deploy.sh pre-flight
aborts (never auto-fixes) on any conflict, and the operator creates the prod `.env` by
hand — the script never writes or copies secrets.
