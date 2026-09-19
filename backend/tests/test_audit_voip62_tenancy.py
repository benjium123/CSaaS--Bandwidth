"""Adversarial audit (session voip-62): tenant isolation on the tables the automatic
guard does NOT cover.

app/db/base.py applies `org_id = <context>` to every ORM query touching a TenantScoped
model, and refuses cross-tenant writes. But LoginEvent, Session and SecurityAlert all
carry an org_id WITHOUT the mixin, so for those the org boundary is whatever the route
writes by hand. These tests probe exactly those hand-written boundaries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.models import LoginEvent
from tests.conftest import auth_headers, create_org, register_and_login


# ======================================================================================
# An admin of workspace A must not see a member's sign-ins to workspace B.
#
# identity.py:315-320 selects `org_id == this org OR user_id IN (members of this org)`.
# The second clause exists for a real reason -- a password/passkey sign-in happens before
# a workspace is chosen and carries org_id = NULL -- but as written it is unconditional,
# so it also drags in events explicitly stamped with ANOTHER org's id.
#
# Attack: Alice consults for two workspaces, evil-corp and bank-co. Evil-corp's admin
# holds members:read. Alice's sign-ins to bank-co are stamped org_id = bank-co, and
# LoginEvent is not TenantScoped, so nothing filters them out.
# ======================================================================================
async def test_org_login_events_do_not_leak_another_orgs_sign_ins(client, session):
    token = await register_and_login(client, "alice@example.com")
    org_a = await create_org(client, token, "Evil Corp")
    org_b = await create_org(client, token, "Bank Co")

    user_id = (
        await session.execute(sa.select(sa.text("id")).select_from(sa.text("users")).limit(1))
    ).scalar_one()

    now = datetime.now(timezone.utc)
    session.add_all(
        [
            # A sign-in that belongs to the OTHER workspace.
            LoginEvent(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=uuid.UUID(org_b["id"]),
                email="alice@example.com",
                at=now,
                ip="203.0.113.9",
                user_agent="bank-co-laptop",
                outcome="sso",
                detail="risk:new_country",
            ),
            # A pre-workspace sign-in, which the admin IS meant to see.
            LoginEvent(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=None,
                email="alice@example.com",
                at=now - timedelta(minutes=1),
                ip="198.51.100.4",
                user_agent="shared-laptop",
                outcome="ok",
                detail=None,
            ),
        ]
    )
    await session.commit()

    r = await client.get(
        "/api/v1/orgs/current/login-events", headers=auth_headers(token, org_a["id"]), params={"limit": 200}
    )
    assert r.status_code == 200, r.text
    agents = [row["user_agent"] for row in r.json()]

    # The pre-workspace sign-in is the whole point of the second clause: keep it.
    assert "shared-laptop" in agents

    assert "bank-co-laptop" not in agents, (
        "workspace A's admin was shown a sign-in stamped with workspace B's org_id "
        f"(IP and risk flags included): {r.json()}"
    )


# ======================================================================================
# The operator KYC document path, pinned as CORRECT (no defect here).
#
# ops.py:277-281 download_document calls kyc_svc.load_for_operator(session, org_id) FIRST,
# which binds the org context (services/kyc.py:515), and only then kyc_documents.get,
# which ALSO checks row.org_id != org_id by hand (services/kyc_documents.py:136-140). So a
# foreign document id is refused twice over: by the automatic tenant filter and by the
# explicit check. This test exists so that removing either layer fails loudly -- dropping
# the load_for_operator call would turn the route into a MissingTenantContextError, and
# dropping the explicit check would leave the filter as the only boundary.
# ======================================================================================
async def test_operator_document_lookup_is_scoped_to_the_named_org(client, session):
    from app.db.base import set_org_context
    from app.models import KycDocument
    from app.services import kyc_documents

    token = await register_and_login(client, "docs@example.com")
    org_a = await create_org(client, token, "Doc Co A")
    org_b = await create_org(client, token, "Doc Co B")
    org_a_id, org_b_id = uuid.UUID(org_a["id"]), uuid.UUID(org_b["id"])

    doc_id = uuid.uuid4()
    set_org_context(session, org_a_id)
    session.add(
        KycDocument(
            id=doc_id,
            org_id=org_a_id,
            kind="registration",
            storage_key=f"org/{org_a_id}/kyc/{doc_id}",
            content_type="application/pdf",
            size_bytes=10,
            sha256="0" * 64,
            filename="cert.pdf",
        )
    )
    await session.commit()
    session.expunge_all()

    from app.errors import NotFoundError

    # The route's own sequence: bind the named org, then fetch.
    set_org_context(session, org_a_id)
    got = await kyc_documents.get(session, org_a_id, doc_id)
    assert got.id == doc_id

    # An operator aiming the same document id at a DIFFERENT org gets nothing. This is the
    # cross-tenant read the ops console must never perform.
    session.expunge_all()
    set_org_context(session, org_b_id)
    try:
        await kyc_documents.get(session, org_b_id, doc_id)
    except NotFoundError:
        pass
    else:
        raise AssertionError("a document was returned for the wrong org_id")
