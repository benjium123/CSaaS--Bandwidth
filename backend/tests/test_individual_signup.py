"""Self-serve signup can mint an individual (personal) workspace, not just a business one.

``RegisterIn.account_type`` is a ``Literal["business", "individual"]`` defaulting to
``"business"``; the created org carries the same value on ``Org.account_type`` (DB-checked
by ``ck_orgs_account_type``) and it is echoed back on every ``MembershipOut`` (registration
response and GET /me). An individual signup must supply a non-blank, stripped ``full_name``
*before* any user row is written (a blank one is rejected with 422) and names the workspace
after that person, ignoring ``company_name``. Redemption of an invite never consults the
requested type or the individual name rule: it joins the invite's org (keeping that org's
type) and returns the same empty-membership shape as before.

Two DB-level regressions back this: the model metadata default/constraint (async, via the
real session) and migration 0057 itself, executed against an isolated in-memory SQLite
database holding one legacy row.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models import Org, User
from tests.conftest import auth_headers, confirm_registered_email, register_and_login

pytestmark = pytest.mark.usefixtures("paid_seats")  # adds members; not about seats

PASSWORD = "correct-horse-battery"


async def _post_register(client, email: str, **extra):
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, **extra},
    )


async def _register(client, email: str, **extra) -> dict:
    r = await _post_register(client, email, **extra)
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _me(client, email: str) -> dict:
    token = await _login(client, email)
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _count(session, model) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count()).select_from(model).execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def _stored_org_type(session, slug: str) -> str:
    """Read the persisted type straight from the DB (slug is unique and a plain string)."""
    return (
        await session.execute(
            sa.select(Org.account_type)
            .where(Org.slug == slug)
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()


async def _mint_invite(client, owner_token: str, org_id: str, email: str) -> str:
    r = await client.post(
        "/api/v1/orgs/current/invites",
        json={"email": email, "role_name": "agent"},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


# ----------------------------------------------------------------------------------
# Individual signup: saved type, personal naming, and /me round-trip
# ----------------------------------------------------------------------------------
async def test_individual_signup_saves_type_and_personal_name(client, session):
    reg = await _register(
        client,
        "ada@acme-widgets.com",
        account_type="individual",
        full_name="  Ada Lovelace  ",
        # A company_name must NOT steer the individual workspace's name.
        company_name="Acme Widgets",
    )
    assert len(reg["memberships"]) == 1
    m = reg["memberships"][0]
    assert m["account_type"] == "individual"
    # Stripped before use, and the person - not the company - names the workspace.
    assert m["org_name"] == "Ada Lovelace"
    assert m["role_name"] == "owner"

    me = await _me(client, "ada@acme-widgets.com")
    assert len(me["memberships"]) == 1
    assert me["memberships"][0]["org_id"] == m["org_id"]
    assert me["memberships"][0]["account_type"] == "individual"
    assert me["memberships"][0]["org_name"] == "Ada Lovelace"

    assert await _stored_org_type(session, m["org_slug"]) == "individual"


# ----------------------------------------------------------------------------------
# Business is the default when omitted, and stays business when explicit
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("email", ["founder@gmail.com", "founder@company.com"])
@pytest.mark.parametrize("legacy_type", ["business", "individual"])
async def test_all_signup_types_use_personal_verification(client, email, legacy_type):
    result = await _register(client, email, full_name="New Customer", account_type=legacy_type)
    assert result["memberships"][0]["account_type"] == "individual"
    assert result["memberships"][0]["org_name"] == "New Customer"


# ----------------------------------------------------------------------------------
# Individual signup needs a nonblank stripped full_name, checked before any user write
# ----------------------------------------------------------------------------------
async def test_individual_signup_requires_full_name_and_writes_nothing(client, session):
    users_before = await _count(session, User)
    orgs_before = await _count(session, Org)

    attempts = (
        ("solo-missing@acme-widgets.com", {}),  # omitted entirely
        ("solo-empty@acme-widgets.com", {"full_name": ""}),  # empty
        ("solo-blank@acme-widgets.com", {"full_name": "   "}),  # whitespace only -> strips empty
    )
    for email, extra in attempts:
        r = await _post_register(client, email, account_type="individual", **extra)
        # ValidationFailedError.http_status is 422 (app/errors.py) - exactly, not 400.
        assert r.status_code == 422, (email, r.status_code, r.text)

    # Rejected *before* the user (and therefore the org) is persisted.
    assert await _count(session, User) == users_before
    assert await _count(session, Org) == orgs_before


# ----------------------------------------------------------------------------------
# A type outside the Literal is a validation error and persists nothing
# ----------------------------------------------------------------------------------
async def test_invalid_account_type_is_422_and_writes_nothing(client, session):
    users_before = await _count(session, User)

    for value in ("team", "Business", "individual "):
        r = await _post_register(
            client, f"weird-{uuid.uuid4().hex[:8]}@example.com", account_type=value
        )
        assert r.status_code == 422, (value, r.status_code, r.text)

    assert await _count(session, User) == users_before


# ----------------------------------------------------------------------------------
# Redeeming an invite to a BUSINESS org ignores an individual request + blank name
# ----------------------------------------------------------------------------------
async def test_invite_to_business_org_ignores_individual_request(client, session):
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    owner_token = await register_and_login(client, owner_email)
    owner_org = (await _me(client, owner_email))["memberships"][0]
    legacy_org = await session.get(Org, uuid.UUID(owner_org["org_id"]))
    legacy_org.account_type = "business"
    await session.commit()

    invitee_email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    invite_token = await _mint_invite(client, owner_token, owner_org["org_id"], invitee_email)

    orgs_before = await _count(session, Org)
    reg = await _register(
        client,
        invitee_email,
        invite_token=invite_token,
        account_type="individual",
        full_name="",  # the individual name rule must not apply on the invite branch
    )
    # Unchanged shape: the invite carries the org, so registration returns neither
    # memberships nor permissions.
    assert reg["memberships"] == []
    assert reg["permissions"] == []

    me = await _me(client, invitee_email)
    assert len(me["memberships"]) == 1
    assert me["memberships"][0]["org_id"] == owner_org["org_id"]
    assert me["memberships"][0]["account_type"] == "business"
    assert await _count(session, Org) == orgs_before


# ----------------------------------------------------------------------------------
# Redeeming an invite to an INDIVIDUAL org ignores a business request, keeps the type
# ----------------------------------------------------------------------------------
async def test_invite_to_individual_org_keeps_individual_type(client, session):
    owner_email = f"solo-{uuid.uuid4().hex[:8]}@example.com"
    owner_reg = await _register(
        client, owner_email, account_type="individual", full_name="Ada Solo"
    )
    owner_org = owner_reg["memberships"][0]
    assert owner_org["account_type"] == "individual"
    await confirm_registered_email(client, owner_email)
    owner_token = await _login(client, owner_email)

    invitee_email = f"invitee-{uuid.uuid4().hex[:8]}@example.com"
    invite_token = await _mint_invite(client, owner_token, owner_org["org_id"], invitee_email)

    orgs_before = await _count(session, Org)
    reg = await _register(client, invitee_email, invite_token=invite_token, account_type="business")
    assert reg["memberships"] == []
    assert reg["permissions"] == []

    me = await _me(client, invitee_email)
    assert len(me["memberships"]) == 1
    assert me["memberships"][0]["org_id"] == owner_org["org_id"]
    assert me["memberships"][0]["account_type"] == "individual"
    assert await _count(session, Org) == orgs_before


# ----------------------------------------------------------------------------------
# DB model regression: account_type defaults to business and the check constraint bites
# ----------------------------------------------------------------------------------
async def test_org_account_type_defaults_to_business_and_is_constrained(session):
    org = Org(name="Solo", slug=f"solo-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    assert org.account_type == "business"

    with pytest.raises(sa.exc.IntegrityError):
        await session.execute(sa.text("UPDATE orgs SET account_type = 'nonsense'"))
    await session.rollback()


# ==================================================================================
# Migration 0057 regression - the executed DDL, not just Base.metadata
# ==================================================================================
_BACKEND = Path(__file__).resolve().parent.parent
MIGRATION_0057 = _BACKEND / "migrations" / "versions" / "0057_individual_accounts.py"


def _load_migration_0057():
    spec = importlib.util.spec_from_file_location("_migration_0057", MIGRATION_0057)
    assert spec is not None and spec.loader is not None, MIGRATION_0057
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration_step(module, name: str, ctx) -> None:
    """Run ``module.<name>()`` with a real Operations installed as ``alembic.op``.

    ``Operations.context`` (a context manager) installs the module-level ``op`` proxy
    for the duration of the block, so the revision's ``op.*`` calls dispatch to the
    Operations bound to this connection.
    """
    with Operations.context(ctx):
        getattr(module, name)()


def test_migration_0057_account_type_upgrade_and_downgrade():
    module = _load_migration_0057()

    engine = sa.create_engine("sqlite://")
    try:
        metadata = sa.MetaData()
        sa.Table(
            "orgs",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(63), nullable=False),
        )
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO orgs (id, name, slug) VALUES ('legacy', 'Legacy Co', 'legacy-co')"
                )
            )

        with engine.begin() as conn:
            _run_migration_step(module, "upgrade", MigrationContext.configure(conn))

        with engine.begin() as conn:
            # The pre-existing row is backfilled by the migration.
            assert (
                conn.execute(
                    sa.text("SELECT account_type FROM orgs WHERE id = 'legacy'")
                ).scalar_one()
                == "business"
            )
            # A row that omits the column takes the DB default too.
            conn.execute(
                sa.text("INSERT INTO orgs (id, name, slug) VALUES ('new', 'New Co', 'new-co')")
            )
            assert (
                conn.execute(sa.text("SELECT account_type FROM orgs WHERE id = 'new'")).scalar_one()
                == "business"
            )

        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO orgs (id, name, slug, account_type) "
                        "VALUES ('bad', 'Bad Co', 'bad-co', 'nonsense')"
                    )
                )

        with engine.begin() as conn:
            _run_migration_step(module, "downgrade", MigrationContext.configure(conn))

        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(orgs)"))}
            assert "account_type" not in columns
            # Both rows survive the downgrade; the legacy one is untouched.
            assert conn.execute(sa.text("SELECT COUNT(*) FROM orgs")).scalar_one() == 2
            assert (
                conn.execute(sa.text("SELECT name FROM orgs WHERE id = 'legacy'")).scalar_one()
                == "Legacy Co"
            )
    finally:
        engine.dispose()
