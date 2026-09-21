# Ringlite

**Repo:** https://github.com/benjium123/CSaaS--Bandwidth

Ringlite is a multi-tenant communications platform serving both business and
individual accounts.

## Accounts and access

- **Business accounts** and **individual accounts** are both supported.
- Individual accounts require **Didit** identity verification followed by
  **mandatory admin approval** before activation.
- Individual accounts are **calling only**: no SMS and no MMS.

## Carriers and services

- **Telnyx** is the primary carrier.
- Didit provides identity verification (KYC).

## Target domain

The target public domain is <https://ringlite.io>. This change does **not**
deploy anything; the domain cutover is pending operator action. See
[docs/RINGLITE_DOMAIN_CUTOVER.md](docs/RINGLITE_DOMAIN_CUTOVER.md) for the
cutover plan and [docs/RUNBOOK.md](docs/RUNBOOK.md) for operations.

## Identifiers

Existing repository, package, container, database and environment identifiers
are retained. Internal compatibility identifiers are preserved while the
public product name changes to Ringlite. Follow `.env.example` for the current
environment variable names and expected values.

## Start here

| Read | For |
|---|---|
| **`docs/PROGRESS.md`** | Historical phase chronology and decision log; verify current state. |
| `docs/ARCHITECTURE.md` | The settled decisions and why. Do not relitigate without hitting the stated condition. |
| `docs/PHASES.md` | The 15 phases and their gates. |
| `docs/WORKSTREAMS.md` | Who owns which files, and the cross-cutting invariants. |
| `docs/SPEC.md` | Full feature scope mapped to phases, including what's explicitly out of v1. |
| `docs/DELEGATION.md` | Which model tier does what on this codebase. |
| `docs/research/` | The evidence behind the decisions. Read only the relevant one. |
| [docs/RINGLITE_DOMAIN_CUTOVER.md](docs/RINGLITE_DOMAIN_CUTOVER.md) | Domain cutover plan (pending operator). |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operations runbook. |

## Setup

Copy the example environment file and fill in local values:

```sh
cp .env.example .env
```

`.env` is gitignored; never commit secrets. `.env.example` is the authoritative
list of environment variables. The keys required for the primary Telnyx path
are:

- `TELNYX_API_KEY`
- `TELNYX_MESSAGING_PROFILE_ID`
- `TELNYX_VOICE_CONNECTION_ID`
- `TELNYX_PUBLIC_KEY` for webhook signature verification

Core application keys:

- `JWT_SECRET`
- `SESSION_SECRET`
- `CREDENTIAL_ENCRYPTION_KEY`
- `CREDENTIALS_MASTER_KEY`

For the Ringlite target deployment, `PUBLIC_BASE_URL` and `PUBLIC_WEB_URL`
should point at <https://ringlite.io>. Local development keeps its existing
local URLs.

## Run it locally

No Docker or Postgres needed for development — that is deliberate (see
`docs/plans/phase-0-plan.md` DR-1).

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e "backend[dev]"    # Windows
# source .venv/bin/activate && pip install -e "backend[dev]"   # macOS/Linux

cd backend
python -m pytest -q      # SQLite in-memory; pg_only tests skip
python -m ruff check .   # includes the Postgres-dialect import ban

DATABASE_URL="sqlite+aiosqlite:///./dev.db" \
JWT_SECRET="$(openssl rand -hex 32)" \
SESSION_SECRET="$(openssl rand -hex 32)" \
python -m uvicorn app.main:app --factory --port 8080
# -> http://localhost:8080/healthz
```

**Note on config precedence:** OS environment variables override `.env`. If a provider
reports as enabled when its `.env` line is blank, check your shell environment.

**Postgres is the merge gate, not SQLite.** CI runs the whole suite against a real
`postgres:16` container plus `alembic upgrade head` / `downgrade base` / `upgrade head`.
SQLite is only a fast local proxy.
