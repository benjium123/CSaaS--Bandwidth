import pytest

from app.services import mailer
from tests.conftest import (
    auth_headers,
    confirm_registered_email,
    latest_email_code,
    login_with_email_code,
)


@pytest.mark.parametrize("account_type", ["individual", "business"])
async def test_email_is_first_gate_and_code_is_single_use(client, account_type):
    email = f"new-{account_type}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Ada Smith",
            "account_type": account_type,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["email_verification_required"] is True
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    token = login.json()["access_token"]
    headers = auth_headers(token, response.json()["memberships"][0]["org_id"])
    blocked = await client.get("/api/v1/kyc/profile", headers=headers)
    assert blocked.status_code == 403
    assert "email_verification_required" in blocked.text
    code = latest_email_code(email)

    # The code is bound to the signed-in account: no session, no confirmation.
    assert (
        await client.post("/api/v1/auth/confirm-email", json={"code": code})
    ).status_code in (401, 403)
    wrong = "000000" if code != "000000" else "111111"
    assert (
        await client.post("/api/v1/auth/confirm-email", json={"code": wrong}, headers=headers)
    ).status_code == 401
    assert (
        await client.post("/api/v1/auth/confirm-email", json={"code": code}, headers=headers)
    ).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()[
        "email_verification_required"
    ] is False
    assert (await client.get("/api/v1/kyc/profile", headers=headers)).status_code == 200


@pytest.fixture
async def on_client(engine):
    """The production default: confirming the address turns email codes on."""
    import httpx

    from app.main import create_app
    from tests.conftest import make_settings

    application = create_app(make_settings(email_2fa_on_verify=True))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_confirming_turns_email_codes_on_and_shows_recovery_codes_once(on_client):
    import sqlalchemy as sa

    from app.db.session import get_sessionmaker
    from app.models import AccountAuditEntry, User

    email = "fresh@example.com"
    r = await on_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "full_name": "Fresh"},
    )
    assert r.status_code == 201, r.text
    login = await on_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    headers = auth_headers(login.json()["access_token"])
    r = await on_client.post(
        "/api/v1/auth/confirm-email", json={"code": latest_email_code(email)}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_2fa_enabled"] is True

    me = (await on_client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["email_2fa_enabled"] is True
    assert me["second_factor_required"] is False
    assert me["needs_recovery_codes"] is True

    # The confirmation proved the inbox, so the step-up behind code generation passes.
    r = await on_client.post("/api/v1/auth/recovery-codes", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["codes"]) == 10
    # Generating is not enough: closing the tab before saving them must not skip the screen.
    me = (await on_client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["needs_recovery_codes"] is True
    r = await on_client.post("/api/v1/auth/recovery-codes/acknowledge", headers=headers)
    assert r.status_code == 200, r.text
    me = (await on_client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["needs_recovery_codes"] is False

    again = await on_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    body = again.json()
    assert body["requires_2fa"] is True
    assert body["methods"] == ["email"]
    assert body["recovery_codes_available"] is True

    async with get_sessionmaker()() as session:
        user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
        entries = (
            await session.execute(
                sa.select(AccountAuditEntry).where(
                    AccountAuditEntry.user_id == user.id,
                    AccountAuditEntry.action == "email_2fa.enabled",
                )
            )
        ).scalars().all()
    assert [e.detail for e in entries] == [{"via": "email_verification"}]


async def test_existing_user_without_codes_is_asked_once_after_2fa_sign_in(on_client):
    email = "returning@example.com"
    await on_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "full_name": "Back"},
    )
    await confirm_registered_email(on_client, email)
    first = await on_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert first.json()["recovery_codes_available"] is False
    token = await login_with_email_code(on_client, email)
    me = (await on_client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert me["needs_recovery_codes"] is True


async def test_confirm_does_not_turn_email_codes_on_when_flag_off(client):
    email = "flagoff@example.com"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "full_name": "Off"},
    )
    r = await confirm_registered_email(client, email)
    assert r.json()["email_2fa_enabled"] is False
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert login.json()["access_token"]
    me = (
        await client.get("/api/v1/auth/me", headers=auth_headers(login.json()["access_token"]))
    ).json()
    assert me["needs_recovery_codes"] is False


@pytest.mark.parametrize("response_code, expected", [(200, True), (422, False)])
async def test_resend_delivery_reports_provider_failure(monkeypatch, response_code, expected):
    import httpx

    from tests.conftest import make_settings

    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(response_code, json={"id": "email_test"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = make_settings(
        app_env="development",
        resend_api_key="test-key",
        resend_from="Ringlite <no-reply@example.com>",
    )
    assert (
        await mailer.send(
            settings, ["ada@example.com"], "Confirm", "https://example.com/confirm-email?token=test"
        )
        is expected
    )
    assert str(calls[0].url) == "https://api.resend.com/emails"


@pytest.mark.parametrize("response_code, expected", [(202, True), (422, False), (503, False)])
async def test_telnyx_confirmation_delivery(monkeypatch, response_code, expected):
    import json

    import httpx

    from tests.conftest import make_settings

    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(response_code, json={"data": {"id": "test", "status": "queued"}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = make_settings(
        app_env="development", telnyx_api_key="test-key", telnyx_email_from="ringlite@example.com"
    )
    assert (
        await mailer.send(
            settings, ["ada@example.com"], "Confirm", "https://example.com/confirm-email?token=test"
        )
        is expected
    )
    assert str(calls[0].url) == "https://api.telnyx.com/v2/email_messages"
    payload = json.loads(calls[0].content)
    assert payload["from"] == "ringlite@example.com"
    assert payload["to"] == ["ada@example.com"]
    assert "confirm-email?token=test" in payload["text_body"]
    assert "<a href=" in payload["html_body"]


@pytest.mark.parametrize("resend_code, urls", [
    (200, ["https://api.resend.com/emails"]),
    (422, ["https://api.resend.com/emails", "https://api.telnyx.com/v2/email_messages"]),
])
async def test_resend_is_primary_and_telnyx_the_fallback(monkeypatch, resend_code, urls):
    import httpx

    from tests.conftest import make_settings

    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "resend" in str(request.url):
            return httpx.Response(resend_code, json={"id": "email_test"})
        return httpx.Response(202, json={"data": {"id": "tx_1"}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = make_settings(
        app_env="development",
        resend_api_key="test-key",
        resend_from="Ringlite <no-reply@example.com>",
        telnyx_api_key="test-key",
        telnyx_email_from="ringlite@example.com",
    )
    assert await mailer.send(settings, ["ada@example.com"], "Code", "Your code is 123456", follow_up=False)
    assert calls == urls


async def test_resend_stops_at_the_daily_cap_and_telnyx_takes_over(monkeypatch):
    import httpx

    from tests.conftest import make_settings

    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append("resend" if "resend" in str(request.url) else "telnyx")
        if "resend" in str(request.url):
            return httpx.Response(200, json={"id": "email_test"})
        return httpx.Response(202, json={"data": {"id": "tx_1"}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(mailer, "_resend_local", {})
    monkeypatch.setattr(mailer, "RESEND_DAILY_CAP", 2)
    settings = make_settings(
        app_env="development",
        redis_url="",
        resend_api_key="test-key",
        resend_from="Ringlite <no-reply@example.com>",
        telnyx_api_key="test-key",
        telnyx_email_from="ringlite@example.com",
    )
    for _ in range(3):
        assert await mailer.send(settings, ["ada@example.com"], "Code", "Your code is 1", follow_up=False)
    assert calls == ["resend", "resend", "telnyx"]
