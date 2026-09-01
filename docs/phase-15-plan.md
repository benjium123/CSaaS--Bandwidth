# Phase 15 — Departments + Tiered Inbox Access

## Goal
Tiered inbox access: admin sees everything; departments own numbers their members
inherit; individual users can be granted specific inboxes (member = read+send/dial,
viewer = read-only). Enforced for SMS threads AND calls: visibility, sending, dialing,
ring fan-out. Fail-closed: no grant → no access (admins bypass via `inboxes:admin`).

## Already done by Fable (Tier 1 — DO NOT TOUCH)
- `app/models/inboxes.py`: `Department`, `DepartmentMember`, `Inbox`, `InboxGrant`,
  `GRANTEE_TYPES`, `INBOX_GRANT_ROLES`. Read its docstring — it IS the access contract.
- `app/models/rbac.py`: new permissions `inboxes:admin`, `departments:read`,
  `departments:manage` (system admin gets them via comprehension; agent does NOT).
- `app/models/__init__.py`: exports.
- Migration `migrations/versions/0016_departments_inboxes.py`: tables + one inbox per
  existing number (named by e164, zero grants) + admin permission backfill.

## Allowed Files (implementer may create/edit ONLY these)
CREATE:
- backend/app/services/inbox_access.py
- backend/app/api/routes/departments.py
- backend/app/api/routes/inboxes.py
- backend/tests/test_p15_inbox_access.py
- backend/tests/test_p15_departments_api.py
EDIT:
- backend/app/main.py            (ONLY adding the two include_router lines + imports)
- backend/app/api/routes/messages.py  (thread list filter + send guard)
- backend/app/api/routes/inbox.py     (thread state/read routes: filter + mutation guard)
- backend/app/api/routes/calls.py     (list/detail filter + place-call guard)
- backend/app/api/routes/softphone.py (ring fan-out: members only)
- backend/app/api/routes/numbers.py   (auto-create Inbox when a number is created)

## Forbidden
- app/models/* and migrations/* (Fable-owned, already written)
- app/db/*, app/auth/*, deploy/*, .env, frontend/*
- any file not in Allowed Files. No new dependencies. No function-signature changes to
  functions called outside the allowed files.

## Implementation Notes
1. `services/inbox_access.py` — single source of truth:
   ```python
   @dataclass(frozen=True)
   class InboxAccess:
       is_admin: bool                 # role grants "inboxes:admin" (or wildcard)
       member_e164s: frozenset[str]   # may read + send/dial
       viewer_e164s: frozenset[str]   # read-only (superset semantics: member implies view)
       def can_view(self, e164) -> bool
       def can_use(self, e164) -> bool     # send SMS / place call from this number
   async def resolve_access(session, user, permissions) -> InboxAccess
   ```
   Resolution: admin short-circuits; else join InboxGrant → Inbox → OrgNumber for
   grants where (grantee_type='user' AND grantee_id=user.id) OR (grantee_type=
   'department' AND grantee_id IN user's departments). member beats viewer on conflict.
   All queries run under the session org context (TenantScoped handles org filtering).
2. Enforcement pattern: routes resolve access once per request; filtering =
   `.where(MessageThread.our_e164.in_(...))` unless is_admin; guards raise the same
   403 error type `require_permission` uses (match the existing deny path exactly).
   An inaccessible thread/call DETAIL returns 404 (not 403) — don't leak existence.
3. `routes/departments.py`: GET/POST /api/v1/departments (departments:read /
   departments:manage), PATCH/DELETE /{id}, PUT /{id}/members (replace member list).
   DELETE must also delete that department's InboxGrant rows (no FK on grantee_id —
   deliberate, see model docstring) and its member rows.
4. `routes/inboxes.py`: GET /api/v1/inboxes → inboxes the caller can see, each with
   {id, name, color, e164, number_id, my_role: "admin"|"member"|"viewer"}; admin sees
   all. PATCH /{id} (name/color) and GET/PUT /{id}/grants require `inboxes:admin`.
   PUT grants replaces the grant list atomically; validate grantee existence and
   grantee_type/role against model constants.
5. Ring fan-out (softphone.py): a ring event for our number X goes ONLY to admins and
   users whose access has X in member_e164s (viewers don't get answerable rings).
6. numbers.py: wherever an OrgNumber row is created, create the Inbox (name=e164) in
   the same transaction. Missing inbox elsewhere = bug, do not lazily create.
7. Audit: use the existing audit service (services/audit.py) for department/grant
   mutations, matching how other routes log.

## Test Spec
Unit (test_p15_inbox_access.py):
- [ ] admin (inboxes:admin) → is_admin, can_use anything
- [ ] direct user grant member → can_view + can_use that e164 only
- [ ] direct user grant viewer → can_view only, can_use False
- [ ] department grant member + user in dept → access; user NOT in dept → none
- [ ] member-beats-viewer when both paths exist
- [ ] no grants → empty sets (fail-closed)
- [ ] org isolation: grants in org B invisible under org A context
Integration (test_p15_departments_api.py + additions):
- [ ] agent role: GET /departments 200 (departments:read? NO — agent lacks it → 403);
      admin: full CRUD ok
- [ ] PUT /departments/{id}/members replaces list; removed member loses inbox access
- [ ] DELETE department removes its grants (access revoked)
- [ ] GET /inboxes as granted agent → only granted inboxes with correct my_role
- [ ] GET /threads filtered by access; ungranted thread detail → 404
- [ ] POST send from ungranted/viewer number → 403; member → accepted
- [ ] GET /calls filtered; POST /calls from ungranted number → 403
- [ ] number create → inbox auto-created (name = e164)
- [ ] existing full suite stays green

Pass criteria: ALL new tests + the ENTIRE existing backend suite green
(`../.venv/Scripts/python.exe -m pytest tests -q` from backend/). Report exact
pass/fail counts and any output verbatim.

## Deploy
yes (after Opus review + Fable sign-off; deploy via ./deploy/deploy.sh which runs
alembic upgrade head).
