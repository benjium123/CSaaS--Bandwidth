import re

import pytest

from app.services import mailer
from tests.conftest import auth_headers


@pytest.mark.parametrize("account_type", ["individual", "business"])
async def test_email_is_first_gate_and_link_is_single_use(client, account_type):
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
    headers = auth_headers(
        login.json()["access_token"], response.json()["memberships"][0]["org_id"]
    )
    blocked = await client.get("/api/v1/kyc/profile", headers=headers)
    assert blocked.status_code == 403
    assert "email_verification_required" in blocked.text
    message = next(m for m in reversed(mailer.outbox) if email in m["To"])
    token = re.search(
        r"token=([A-Za-z0-9_-]+)", message.get_body(preferencelist=("plain",)).get_content()
    ).group(1)
    assert (
        await client.post("/api/v1/auth/confirm-email", json={"token": token})
    ).status_code == 200
    assert (
        await client.post("/api/v1/auth/confirm-email", json={"token": token})
    ).status_code == 422
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()[
        "email_verification_required"
    ] is False
    assert (await client.get("/api/v1/kyc/profile", headers=headers)).status_code == 200


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
