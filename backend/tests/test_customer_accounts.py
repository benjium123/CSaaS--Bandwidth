# ruff: noqa: F811
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import FraudIdentifier, KycPerson, Org, OrgNumber, User
from tests.conftest import auth_headers, register_and_login
from tests.test_p41_kyc import _make_operator
from tests.test_p41_kyc import kyc_app as kyc_app
from tests.test_p41_kyc import kyc_settings as kyc_settings


async def test_directory_delete_and_email_ban(kyc_app, session):
    client, *_ = kyc_app
    token = await register_and_login(client, "remove-me@example.com")
    admin = await _make_operator(client, session, "directory-admin@example.com")
    h = auth_headers(admin)
    listing = await client.get("/api/v1/ops/customer-accounts", headers=h)
    assert listing.status_code == 200, listing.text
    account = next(a for a in listing.json()["accounts"] if a["email"] == "remove-me@example.com")
    assert account["workspaces"][0]["status"] == "draft"
    uid = account["id"]
    detail = await client.get(f"/api/v1/ops/customer-accounts/{uid}", headers=h)
    assert detail.status_code == 200, detail.text
    assert not detail.json()["blockers"]
    payload = {
        "confirmation": "remove-me@example.com",
        "reason": "Test deletion",
        "identifiers": [detail.json()["identifiers"][0]["key"]],
    }
    bad = await client.post(
        f"/api/v1/ops/customer-accounts/{uid}/delete",
        headers=h,
        json={**payload, "confirmation": "wrong"},
    )
    assert bad.status_code == 422
    denied = await client.post(
        f"/api/v1/ops/customer-accounts/{uid}/delete", headers=auth_headers(token), json=payload
    )
    assert denied.status_code == 403
    deleted = await client.post(
        f"/api/v1/ops/customer-accounts/{uid}/delete", headers=h, json=payload
    )
    assert deleted.status_code == 204, deleted.text
    session.expire_all()
    assert (
        await session.scalar(sa.select(User).where(User.email == "remove-me@example.com")) is None
    )
    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token))).status_code == 401
    assert (
        await session.scalar(sa.select(FraudIdentifier).where(FraudIdentifier.kind == "email"))
        is not None
    )
    retry = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "REMOVE-ME@example.com",
            "password": "correct-horse-battery",
            "full_name": "New name",
            "account_type": "individual",
        },
    )
    assert retry.status_code == 422, retry.text
    protected = next(a for a in listing.json()["accounts"] if a["is_operator"])
    assert (
        await client.get(f"/api/v1/ops/customer-accounts/{protected['id']}", headers=h)
    ).status_code == 422


async def test_identity_and_phone_bans_survive_deletion(kyc_app, session):
    import uuid

    from app.models import KycProfile
    from app.services import ban_list, kyc

    client, *_ = kyc_app
    token = await register_and_login(client, "identity-delete@example.com")
    admin = await _make_operator(client, session, "identity-admin@example.com")
    user = await session.scalar(sa.select(User).where(User.email == "identity-delete@example.com"))
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    org_id = uuid.UUID(me["memberships"][0]["org_id"])
    set_org_context(session, org_id)
    profile = await session.scalar(sa.select(KycProfile))
    profile.business_phone = "+12125551234"
    digest = kyc.identity_hash("Ada", "Smith", "1980-01-01")
    session.add(
        KycPerson(
            org_id=org_id,
            user_id=user.id,
            role="owner",
            full_name="Ada Smith",
            status="verified",
            identity_provider="didit",
            identity_hash=digest,
        )
    )
    await session.commit()
    path = f"/api/v1/ops/customer-accounts/{user.id}"
    data = (await client.get(path, headers=auth_headers(admin))).json()
    assert {i["kind"] for i in data["identifiers"]} == {"email", "phone", "person"}
    response = await client.post(
        path + "/delete",
        headers=auth_headers(admin),
        json={
            "confirmation": user.email,
            "reason": "Fraud review",
            "identifiers": [i["key"] for i in data["identifiers"]],
        },
    )
    assert response.status_code == 204, response.text
    assert (
        len(
            await ban_list.matches(
                session, [("person", digest), ban_list.identifier("phone", "+1 (212) 555-1234")]
            )
        )
        == 2
    )


async def test_deletion_blocks_live_numbers(kyc_app, session):
    import uuid

    client, *_ = kyc_app
    token = await register_and_login(client, "live-number@example.com")
    admin = await _make_operator(client, session, "live-admin@example.com")
    user = await session.scalar(sa.select(User).where(User.email == "live-number@example.com"))
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    org_id = uuid.UUID(me["memberships"][0]["org_id"])
    set_org_context(session, org_id)
    session.add(OrgNumber(org_id=org_id, e164="+12125559999", carrier="telnyx", status="active"))
    await session.commit()
    path = f"/api/v1/ops/customer-accounts/{user.id}"
    detail = (await client.get(path, headers=auth_headers(admin))).json()
    assert detail["blockers"]
    response = await client.post(
        path + "/delete",
        headers=auth_headers(admin),
        json={"confirmation": user.email, "reason": "Delete"},
    )
    assert response.status_code == 422
    assert await session.get(Org, org_id) is not None


async def test_blacklist_disables_login_and_unknown_identifiers_are_rejected(kyc_app, session):
    client, *_ = kyc_app
    token = await register_and_login(client, "block-now@example.com")
    admin = await _make_operator(client, session, "block-admin@example.com")
    user = await session.scalar(sa.select(User).where(User.email == "block-now@example.com"))
    path = f"/api/v1/ops/customer-accounts/{user.id}"
    headers = auth_headers(admin)
    detail = (await client.get(path, headers=headers)).json()
    bad = await client.post(
        path + "/blacklist",
        headers=headers,
        json={"reason": "Review", "identifiers": ["person:invented"]},
    )
    assert bad.status_code == 422
    result = await client.post(
        path + "/blacklist",
        headers=headers,
        json={"reason": "Review", "identifiers": [detail["identifiers"][0]["key"]]},
    )
    assert result.status_code == 204, result.text
    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token))).status_code == 401
    session.expire_all()
    assert (
        await session.scalar(sa.select(User).where(User.email == "block-now@example.com"))
    ).is_active is False


async def test_blocked_phone_cannot_be_used_in_new_application(kyc_app, session):
    from app.services import ban_list
    from tests.test_short_verification import FORM

    client, *_ = kyc_app
    token = await register_and_login(client, "new-phone@example.com")
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    await ban_list.add(session, kind="phone", value=FORM["phone"], reason="Blocked")
    await session.commit()
    response = await client.put(
        "/api/v1/kyc/application",
        headers=auth_headers(token, me["memberships"][0]["org_id"]),
        json=FORM,
    )
    assert response.status_code == 422, response.text


async def test_directory_includes_unconfirmed_signups(kyc_app, session):
    client, *_ = kyc_app
    admin = await _make_operator(client, session, "unconfirmed-admin@example.com")
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "unconfirmed@example.com",
            "password": "correct-horse-battery",
            "full_name": "Waiting",
            "account_type": "individual",
        },
    )
    assert registered.status_code == 201, registered.text
    listed = await client.get(
        "/api/v1/ops/customer-accounts?q=unconfirmed%40", headers=auth_headers(admin)
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["accounts"][0]["email_verified"] is False


async def test_verified_didit_identity_ban_blocks_resubmission(
    kyc_app, session, kyc_settings, monkeypatch
):
    from app.services import ban_list
    from tests.test_individual_kyc_api import (
        _accept_agreement,
        _org_id,
        _register_individual,
        _verify_owner,
    )
    from tests.test_short_verification import FORM

    client, *_ = kyc_app
    token = await _register_individual(client, "returning-person@example.com")
    org = await _org_id(client, token)
    headers = auth_headers(token, org)
    saved = await client.put("/api/v1/kyc/application", headers=headers, json=FORM)
    assert saved.status_code == 200
    person_id = saved.json()["persons"][0]["id"]
    await _verify_owner(client, session, kyc_settings, monkeypatch, headers, org, person_id)
    await _accept_agreement(client, headers)
    import uuid

    set_org_context(session, uuid.UUID(org))
    person = await session.get(KycPerson, uuid.UUID(person_id))
    await ban_list.add(
        session,
        kind="person",
        value_hash=person.identity_hash,
        reason="Previously blacklisted identity",
    )
    await session.commit()
    response = await client.post("/api/v1/kyc/submit", headers=headers)
    assert response.status_code == 422, response.text
    assert "account_blacklisted" in response.text
