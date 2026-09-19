"""Owner-only authorization: refusal tests.

Pins the ONE refusal shape for the re-gated routes - 403 with
``error.code == "owner_only"`` - and pins the routes that must NOT be owner-only,
including the identity-verification routes a non-owner admin needs in order not to be
permanently locked out of every privileged permission.

Every assertion here is EXACT (status code and error code). ``!= 200`` is never used to
stand in for a status: a route silently disappearing from the app, or moving behind a
different gate, has to fail loudly rather than pass by accident.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.auth.deps import OrgContext, require_owner
from app.db.base import set_org_context
from app.errors import PermissionDeniedError
from app.models import OrgMembership, Role, User
from app.models.rbac import SYSTEM_ROLES
from tests.conftest import auth_headers, create_org, register_and_login
from tests.test_p41_kyc import (  # noqa: F401
    _identity_event,
    _signup_org,
    kyc_app,
    kyc_settings,
)


async def _add_admin_member(client, session, org_id: uuid.UUID, email: str) -> str:
    """Register a second user and give them the org's system `admin` role.

    Returns their access token. `admin` holds every permission except org:delete and
    org:billing - crucially it holds settings:read, org:read and org:update, which is
    exactly why these routes were readable by a non-owner before.
    """
    token = await register_and_login(client, email)
    set_org_context(session, org_id)
    admin_role = (
        await session.execute(sa.select(Role).where(Role.name == "admin"))
    ).scalar_one()
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    set_org_context(session, org_id)
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=admin_role.id)
    )
    await session.commit()
    return token


async def test_billing_read_routes_are_owner_only(client, session):
    owner_token = await register_and_login(client, "owner-billing-read@example.com")
    org = await create_org(client, owner_token, "Owner Billing Read Co")
    org_id = uuid.UUID(org["id"])
    admin_token = await _add_admin_member(
        client, session, org_id, "admin-billing-read@example.com"
    )

    # A random call id that cannot exist. This is what makes the pair asymmetric and
    # therefore probative: the OWNER clears the owner gate and only THEN misses on the
    # lookup (404 not_found), while the ADMIN is stopped by the gate before the lookup is
    # ever consulted (403 owner_only). The gate runs first for the admin; it does not
    # exist for the owner. The handler is never reached on the admin's request.
    missing_call_id = uuid.uuid4()

    # Driven from an explicit list so a route cannot be silently dropped from the gate (or
    # from this test) without the length assertion below failing.
    routes = [
        ("/api/v1/billing/summary", 200),
        ("/api/v1/billing/ledger", 200),
        ("/api/v1/billing/usage", 200),
        (f"/api/v1/billing/usage/calls/{missing_call_id}", 404),
        ("/api/v1/billing/plans", 200),
        ("/api/v1/billing/rates", 200),
        ("/api/v1/billing/payment-methods", 200),
    ]
    assert len(routes) == 7

    for path, owner_status in routes:
        r = await client.get(path, headers=auth_headers(owner_token, org_id))
        assert r.status_code == owner_status, r.text
        if owner_status == 404:
            # The owner passed the gate and fell through to the lookup: not_found, never
            # 403 and never a 500 from the missing row.
            assert r.json()["error"]["code"] == "not_found", r.text

        # The admin acts INSIDE THE OWNER'S WORKSPACE via X-Org-Id. Registering the
        # second user also created a workspace of their own, where they would be an
        # owner - omitting the header would make this refusal pass for the wrong reason.
        r = await client.get(path, headers=auth_headers(admin_token, org_id))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "owner_only", r.text


async def test_billing_money_routes_still_need_the_billing_permission(client, session):
    owner_token = await register_and_login(client, "owner-money@example.com")
    org = await create_org(client, owner_token, "Owner Money Co")
    org_id = uuid.UUID(org["id"])
    admin_token = await _add_admin_member(client, session, org_id, "admin-money@example.com")

    missing_payment_method = uuid.uuid4()
    # (method, path, kwargs, owner_status, owner_error_code). The owner half is driven off
    # the SAME table so the two halves cannot drift apart. `owner_error_code` is None for
    # the one 200 case, whose body has no "error" key at all.
    calls = [
        (
            "POST",
            "/api/v1/billing/topups",
            {"json": {"amount_micros": 25000000}},
            503,
            "feature_unavailable",
        ),
        (
            "POST",
            "/api/v1/billing/payment-methods",
            {"json": {"stripe_payment_method_id": "pm_test_nonowner"}},
            503,
            "feature_unavailable",
        ),
        (
            "DELETE",
            f"/api/v1/billing/payment-methods/{missing_payment_method}",
            {},
            404,
            "not_found",
        ),
        (
            "PATCH",
            "/api/v1/billing/auto-recharge",
            {"json": {"enabled": False}},
            200,
            None,
        ),
    ]

    for method, path, kwargs, _owner_status, _owner_code in calls:
        # The admin is refused, but with `permission_denied` and NOT `owner_only`: these
        # routes are gated by require_permission("org:billing") - the admin role simply
        # lacks that permission - and were deliberately left off require_owner. Asserting
        # the real code keeps the two mechanisms distinguishable, so a future refactor
        # that swapped one gate for the other could not slip past this test.
        r = await client.request(
            method,
            path,
            headers=auth_headers(admin_token, org_id),
            **kwargs,
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "permission_denied", r.text

    for method, path, kwargs, owner_status, owner_code in calls:
        # The owner CLEARS the authorization gate and reaches the handler. The non-200
        # statuses below are the HANDLER talking - Stripe is unconfigured under this
        # fixture (503 feature_unavailable) and the payment method row does not exist
        # (404 not_found) - not the gate refusing. They are pinned EXACTLY for that reason:
        # a future authorization regression that started returning 403 to the owner must
        # fail here rather than hide inside a vague "not 403".
        r = await client.request(
            method,
            path,
            headers=auth_headers(owner_token, org_id),
            **kwargs,
        )
        assert r.status_code == owner_status, r.text
        if owner_code is not None:
            assert r.json()["error"]["code"] == owner_code, r.text


async def test_require_owner_fails_closed(client):
    org_sentinel = object()
    membership_sentinel = object()
    session_sentinel = object()

    def _role(permissions):
        return Role(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="owner-ish",
            permissions=permissions,
        )

    refusals = [
        # An API-key principal: refused outright, even if the wildcard somehow leaked
        # into the transient key role.
        OrgContext(
            org=org_sentinel,
            membership=membership_sentinel,
            role=_role(["*"]),
            session=session_sentinel,
            api_key=object(),
        ),
        # No membership: there is nobody whose ownership this could be.
        OrgContext(
            org=org_sentinel,
            membership=None,
            role=_role(["*"]),
            session=session_sentinel,
        ),
        # A role whose permissions column is NULL.
        OrgContext(
            org=org_sentinel,
            membership=membership_sentinel,
            role=_role(None),
            session=session_sentinel,
        ),
        # A role with an empty permission list.
        OrgContext(
            org=org_sentinel,
            membership=membership_sentinel,
            role=_role([]),
            session=session_sentinel,
        ),
        # The REAL admin permission list: broad, but it contains no wildcard.
        OrgContext(
            org=org_sentinel,
            membership=membership_sentinel,
            role=_role(list(SYSTEM_ROLES["admin"])),
            session=session_sentinel,
        ),
    ]

    for ctx in refusals:
        with pytest.raises(PermissionDeniedError) as exc:
            await require_owner(ctx)
        assert exc.value.code == "owner_only"

    # Happy path: a human member holding the wildcard, with no API key attached.
    wildcard_ctx = OrgContext(
        org=org_sentinel,
        membership=membership_sentinel,
        role=_role(["*"]),
        session=session_sentinel,
        api_key=None,
    )
    assert await require_owner(wildcard_ctx) is wildcard_ctx


async def test_kyc_application_routes_are_owner_only(kyc_app, session):  # noqa: F811
    client, *_ = kyc_app
    owner_token = await register_and_login(client, "owner-kyc-application@example.com")
    # Under kyc_app a bare POST /orgs is a 409 ("only one unverified workspace at a
    # time"), so reuse the workspace registration already created.
    org = await _signup_org(client, owner_token, "Owner KYC Application Co")
    org_id = uuid.UUID(org["id"])
    admin_token = await _add_admin_member(
        client, session, org_id, "admin-kyc-application@example.com"
    )

    # Random {id} values on purpose: the gate must refuse before any lookup, so the
    # response has to be owner_only and not 404. Asserting the code is precisely what
    # proves the lookup never ran.
    missing_id = uuid.uuid4()
    routes = [
        ("GET", "/api/v1/kyc/profile", {}),
        (
            "PUT",
            "/api/v1/kyc/profile/business",
            {"json": {"legal_name": "Owner Co"}},
        ),
        (
            "PUT",
            "/api/v1/kyc/profile/use-case",
            {"json": {"use_case": "customer_support"}},
        ),
        (
            "POST",
            "/api/v1/kyc/persons",
            {"json": {"full_name": "Ada Admin", "email": "ada.admin@example.com"}},
        ),
        (
            "PUT",
            f"/api/v1/kyc/persons/{missing_id}/address",
            {
                "json": {
                    "line1": "1 Main St",
                    "city": "Austin",
                    "region": "TX",
                    "postal_code": "78701",
                    "country": "US",
                }
            },
        ),
        ("DELETE", f"/api/v1/kyc/persons/{missing_id}", {}),
        (
            "POST",
            "/api/v1/kyc/documents",
            {
                "data": {"kind": "formation"},
                "files": {
                    "file": ("x.pdf", b"%PDF-1.4 test", "application/pdf")
                },
            },
        ),
        ("DELETE", f"/api/v1/kyc/documents/{missing_id}", {}),
        ("POST", "/api/v1/kyc/agreement", {"json": {"agreed": True}}),
        ("POST", "/api/v1/kyc/submit", {"json": {}}),
        (
            "POST",
            "/api/v1/kyc/limit-request",
            {"json": {"requested_limit_micros": 5000000}},
        ),
    ]
    assert len(routes) == 11

    for method, path, kwargs in routes:
        # X-Org-Id points the admin at the OWNER's workspace, not at the one registering
        # the second user created for them - there they would be an owner.
        r = await client.request(
            method,
            path,
            headers=auth_headers(admin_token, org_id),
            **kwargs,
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "owner_only", r.text

    r = await client.get("/api/v1/kyc/profile", headers=auth_headers(owner_token, org_id))
    assert r.status_code == 200, r.text

    r = await client.put(
        "/api/v1/kyc/profile/business",
        json={"legal_name": "Owner Co"},
        headers=auth_headers(owner_token, org_id),
    )
    assert r.status_code == 200, r.text


async def test_non_owner_can_still_verify_their_own_identity(kyc_app, session):  # noqa: F811
    """THE LOCKOUT REGRESSION GUARD.

    deps.py::IDENTITY_GATED_PERMISSIONS plus _require_verified_privileged_member refuse a
    non-owner who has not completed an ID + selfie check; /kyc/me/verify and /kyc/step-up
    are the ONLY routes through which a non-owner can complete one. If they were ever made
    owner-only, a non-owner admin would be permanently locked out of every privileged
    permission with no path forward in the product. If this test fails, that is what has
    happened.

    THE SEQUENCING IS THE POINT: /kyc/me/verify is the UNLOCK - it starts the ID + selfie
    check and is what the Stripe webhook then flips to "verified". /kyc/step-up is "prove it
    AGAIN": kyc_step_up.start refuses outright (identity_not_verified) while the person has
    no verified identity hash, so step-up only works AFTER /me/verify has completed. This
    test therefore drives the full path - verify, webhook, step-up - because if /me/verify
    ever became owner-only a non-owner admin could never reach either route, and
    _require_verified_privileged_member would refuse them every identity-gated permission
    forever.
    """
    client, _app, _carrier, created, outcomes = kyc_app
    owner_token = await register_and_login(client, "owner-lockout@example.com")
    org = await _signup_org(client, owner_token, "Owner Lockout Co")
    org_id = uuid.UUID(org["id"])
    admin_token = await _add_admin_member(
        client, session, org_id, "admin-lockout@example.com"
    )

    # This is THE route, and the only route, by which a non-owner completes their own
    # identity check. It is org-scoped: the non-owner acts inside the owner's workspace.
    r = await client.post(
        "/api/v1/kyc/me/verify",
        json={},
        headers=auth_headers(admin_token, org_id),
    )
    assert r.status_code == 200, r.text
    assert "url" in r.json(), r.text

    # The fake Stripe just recorded the verification session it created; drive the webhook
    # that marks it verified. purpose MUST be the literal "kyc_person": that is the branch
    # app/services/kyc.py::handle_identity_event dispatches on for a person check (its only
    # other branch is "step_up"). Any other value logs identity_event_unknown_purpose and
    # silently does nothing - which would leave the admin unverified and 422 the step-up.
    vs = created[-1]
    wh = await _identity_event(
        client,
        outcomes,
        vs["id"],
        status="verified",
        purpose="kyc_person",
        metadata=vs["metadata"],
    )
    # The Stripe webhook route answers 204 No Content.
    assert wh.status_code == 204, wh.text

    # Step-up is user-level, not org-scoped, so no X-Org-Id is sent. It only succeeds now
    # because the webhook above verified the admin.
    r = await client.post(
        "/api/v1/kyc/step-up",
        json={"action": "api_key_create"},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200, r.text
    step_up = r.json()
    assert "url" in step_up, r.text
    step_up_id = step_up["id"]

    r = await client.get(
        f"/api/v1/kyc/step-up/{step_up_id}",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == step_up_id, r.text

    # The admin is STILL refused on the owner-only profile route in the same test, so this
    # cannot pass by the gate having been removed wholesale rather than narrowed.
    r = await client.get("/api/v1/kyc/profile", headers=auth_headers(admin_token, org_id))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "owner_only", r.text
