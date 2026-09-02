# Phase 17 — Provider accounts in-app (per-org, encrypted, 5 providers)

## Goal
An org admin pastes API credentials for Bandwidth, Telnyx, Twilio, Plivo or SignalWire
into the Providers page, clicks Probe, and the backend goes live on that provider for
that org — no `.env` edits, no restarts. Env-var configuration keeps working as the
global fallback (today's deployment is unchanged until an account row exists).

## Pre-dependencies
- New env var `CREDENTIALS_MASTER_KEY` (Fernet key, generated once, added to
  `/opt/csaas/.env` by the operator — deploy.sh never touches .env). If absent, the
  provider-accounts endpoints return 503 "credential storage not configured"; nothing
  else degrades.
- `cryptography` is already a transitive dependency (verify with `pip show cryptography`
  in the venv; if it is not importable, Fable approves adding it explicitly).

## Data model (Fable authors — migration 0018)
`provider_accounts` (TenantScoped):
- `provider` (bandwidth|telnyx|twilio|plivo|signalwire), `label`,
- `credentials_encrypted` (Text, Fernet token of a JSON object),
- `status` (unverified|active|failed|disabled), `last_probe_at`, `last_probe_detail`,
- `created_by` (FK users, SET NULL), unique `(org_id, provider)` for P17 (one account per
  provider per org; multi-account is a later phase).

Per-provider credential schema (field names mirror `app/config.py` so adapters need no
renaming):
- bandwidth: account_id, api_username, api_password*, messaging_application_id,
  voice_application_id, webhook_username, webhook_password*
- telnyx: api_key*, public_key*, messaging_profile_id, voice_connection_id
- twilio: account_sid, auth_token*, messaging_service_sid
- plivo: auth_id, auth_token*, powerpack_uuid
- signalwire: project_id, api_token*, space_url
(* = secret: write-only, never echoed; GET returns `{field: "•••••"}` presence markers.)

## Backend design
1. `app/services/credentials.py` — `encrypt(dict) -> str`, `decrypt(str) -> dict` using
   Fernet(`CREDENTIALS_MASTER_KEY`); raises `CarrierNotConfiguredError`-style 503 when the
   key is missing. Never log plaintext.
2. `app/services/provider_accounts.py` — CRUD + validation against the per-provider
   schema + `probe(account)` reusing `app/providers/probes.py::probe(name, settings_like)`
   by building a settings-like object from the decrypted credentials (same attribute
   names as `Settings`). Probe success → status active; failure → failed + detail.
   Bumps an org-level `accounts_version` (in-memory counter keyed by org) so the registry
   cache invalidates.
3. Per-org registry resolution WITHOUT touching every call site: `build_registry` gains a
   sibling `build_registry_for_org(settings, accounts)` that instantiates adapters from
   DB credentials for providers with an active account, and from env for the rest.
   `app.state.carriers` becomes a `CarrierRegistryProxy` whose attribute access resolves
   the current org from a contextvar set by the auth dependency (`OrgContext`), returning
   the cached per-org registry (LRU keyed by (org_id, accounts_version)), falling back to
   the global env registry when no org context exists (startup, sweeper, webhooks). This
   is the one shared-utility change (Tier 1 flags it); every existing route keeps
   `request.app.state.carriers` unchanged.
4. Webhooks: verification tries the global env registry first (unchanged behaviour),
   then, if the provider has DB accounts, each active account for that provider (bounded
   by the number of orgs with that provider; cached). Org is then resolved as today.
5. Routes `app/api/routes/provider_accounts.py` (settings:read / settings:write):
   `GET /api/v1/provider-accounts`, `POST`, `PATCH /{id}` (partial secret updates:
   omitted secrets keep the stored value), `POST /{id}/probe`, `DELETE /{id}` (sets
   disabled; never hard-deletes). Audit every mutation (no secrets in audit detail).
6. Number ↔ account: `org_numbers` gets `provider_account_id` (nullable FK) in P18; P17
   surfaces "numbers on this provider" by carrier name.

## Frontend
ProvidersPage rework: one card per provider (5), showing status pill
(not configured / unverified / active / failed + probe detail), a form with the
provider's fields (secrets as password inputs that show "stored" placeholder when set),
Save, Probe, Disable; numbers riding on that carrier listed under the card. Reuse the
P16 dark shell and useMutation/status patterns. Only `settings:write` holders see the
forms; `settings:read` sees status only.

## Guardrails (implementer)
ALLOWED: app/services/credentials.py, app/services/provider_accounts.py,
app/api/routes/provider_accounts.py, app/providers/registry.py (proxy + for_org
builder ONLY — adapter constructors untouched), app/auth/deps.py (set the org
contextvar in OrgContext resolution ONLY), app/api/routes/webhooks.py (verification
fallback ONLY), app/main.py (router + proxy install), tests/test_p17_*.py,
frontend/src/pages/ProvidersPage.tsx + its test + frontend/src/api/providers.ts.
FORBIDDEN: models/, migrations/ (Fable), adapters under app/providers/<name>/,
.env, deploy/, everything else.

## Test Spec
- [ ] encrypt/decrypt round-trip; missing master key → 503 on every account endpoint;
      ciphertext never contains plaintext substrings
- [ ] schema validation per provider (missing required field → 422; unknown field → 422)
- [ ] GET never returns secrets (presence markers only); PATCH without a secret keeps it
- [ ] probe success → active with detail; probe failure → failed with detail (mock httpx)
- [ ] registry proxy: org A with DB Telnyx creds resolves a Telnyx adapter built from DB;
      org B without → env fallback; cache invalidates after PATCH; no org context →
      global registry
- [ ] RBAC: settings:read cannot create/patch/probe; audit rows written without secrets
- [ ] tenant isolation on provider_accounts
- [ ] frontend: card states, secret placeholder, PUT/PATCH body shape, probe flow
Pass criteria: all new tests + full backend (~958) + frontend (88) suites green.

## Deploy
yes — but requires the operator to add `CREDENTIALS_MASTER_KEY` to /opt/csaas/.env
first (ask user for approval; generate with `python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())"`).
