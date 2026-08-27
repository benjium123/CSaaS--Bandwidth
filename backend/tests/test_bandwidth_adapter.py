from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
from app.providers.domain import OutboundMessage
from tests.conftest import load_fixture

ACCOUNT = "acct-12345"
APP_ID = "93de2206-9669-4e07-948d-329f4b722ee2"


def build(handler) -> BandwidthMessagingCarrier:
    return BandwidthMessagingCarrier(
        account_id=ACCOUNT,
        api_username="apiuser",
        api_password="apipass",
        application_id=APP_ID,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_send_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json=load_fixture("send-202.json"))

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="corr-1")
    )

    assert result.status == "accepted"
    assert result.provider_message_id == "1755000000000-outbound-bbbb"
    assert result.error is None

    assert ACCOUNT in captured["url"]
    assert captured["url"].endswith(f"/users/{ACCOUNT}/messages")
    expected = base64.b64encode(b"apiuser:apipass").decode()
    assert captured["auth"] == f"Basic {expected}"
    # Bandwidth wants `to` as a LIST even for a single recipient.
    assert captured["body"]["to"] == ["+19725550199"]
    assert captured["body"]["applicationId"] == APP_ID
    assert captured["body"]["tag"] == "corr-1"


@pytest.mark.parametrize(
    "status,body,category,retryable,code",
    [
        (400, {"type": "4720", "description": "bad dest"}, "invalid_request", False, "4720"),
        (400, {"type": "4302", "description": "bad from"}, "invalid_request", False, "4302"),
        (400, {"type": "4476", "description": "unregistered"}, "unregistered", False, "4476"),
        (429, {"type": "4780", "description": "slow down"}, "rate_limited", True, "4780"),
        (503, {"description": "upstream"}, "carrier_transient", True, None),
        (401, {"description": "nope"}, "auth", False, None),
    ],
)
async def test_error_classification(status, body, category, retryable, code):
    carrier = build(lambda request: httpx.Response(status, json=body))
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.category == category
    assert result.error.retryable is retryable
    if code:
        assert result.error.carrier_code == code


async def test_transport_error_is_unreachable_and_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.status == "rejected"
    assert result.error.category == "carrier_unreachable"
    assert result.error.retryable is True


def test_capabilities_are_honest():
    """The adapter must not claim abilities Bandwidth does not have.

    Capabilities are per-INSTANCE, not per-class: whether this deployment may send
    messages depends on whether it was given a messaging application id, and a
    voice-only Bandwidth account is a real, supported configuration.
    """
    caps = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="msg-app"
    ).capabilities
    assert caps.supports_cancel is False
    assert caps.supports_scheduled_send is False
    # 202 Accepted is not delivery. Nothing may pretend otherwise.
    assert caps.sync_delivery_status is False
    assert caps.max_media_bytes == 3_750_000
    assert caps.supports_messaging is True


def test_a_voice_only_account_does_not_claim_messaging():
    """No messaging application id means Bandwidth will not carry texts for this account.
    Saying so up front beats finding out from a rejected send."""
    caps = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id=""
    ).capabilities
    assert caps.supports_messaging is False


# ==================================================================================
# OAuth2. Current Bandwidth accounts issue a Client ID / Client Secret which is NOT
# accepted as HTTP Basic anywhere - verified against a live account, where Basic 401s on
# every host while the same pair mints a working token.
# ==================================================================================
async def test_client_credentials_are_exchanged_for_a_bearer_token():
    import httpx

    from app.providers.bandwidth.auth import BandwidthTokenProvider

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "tok-abc", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = BandwidthTokenProvider("CLI-x", "secret", client=http)
        assert await provider.token() == "tok-abc"
        # Cached: a second call must not hit the token endpoint again. A token lasts an
        # hour; refetching per request adds a round trip to every call.
        assert await provider.token() == "tok-abc"

    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/oauth2/token"
    assert b"grant_type=client_credentials" in seen[0].content


async def test_a_rejected_client_credential_says_so_clearly():
    import httpx
    import pytest as _pytest

    from app.providers.bandwidth.auth import BandwidthAuthError, BandwidthTokenProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "The credentials provided were invalid."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = BandwidthTokenProvider("CLI-x", "bad", client=http)
        with _pytest.raises(BandwidthAuthError) as exc:
            await provider.token()
    assert "invalid" in str(exc.value).lower()


async def test_a_stale_token_is_refreshed_exactly_once_on_401():
    """Tokens die early - revoked, rotated, clock drift. One forced refresh turns a
    mysterious mid-call 401 into a self-healing retry; retrying forever would turn a
    genuinely bad credential into a hot loop against the identity server."""
    import httpx

    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
    from app.providers.domain import OutboundMessage

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token"):
            calls.append("token")
            return httpx.Response(200, json={"access_token": "t2", "expires_in": 3600})
        calls.append("send")
        # First send is rejected as if the cached token had just been revoked.
        if calls.count("send") == 1:
            return httpx.Response(401, json={"type": "unauthorized"})
        return httpx.Response(202, json={"id": "m-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = BandwidthMessagingCarrier(
            account_id="9903389",
            api_username="CLI-x",
            api_password="s",
            application_id="app",
            auth_mode="oauth2",
            client=http,
        )
        result = await carrier.send_message(
            OutboundMessage(to="+12145550100", from_="+19725550100", text="hi")
        )

    assert result.status == "accepted"
    assert calls.count("send") == 2, "exactly one retry after the forced refresh"
    assert calls.count("token") == 2, "the token was refreshed once, not on a loop"


async def test_oauth2_accounts_use_a_bearer_header_not_basic_credentials():
    """media_auth returns a Basic PAIR; under OAuth2 there is no pair to return, and
    handing back the client secret as a password would leak it to the media host."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    oauth = BandwidthMessagingCarrier(
        account_id="a", api_username="CLI-x", api_password="secret",
        application_id="app", auth_mode="oauth2",
    )
    assert oauth.media_auth("https://media.bandwidth.com/x.jpg") is None

    legacy = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p",
        application_id="app", auth_mode="basic",
    )
    assert legacy.media_auth("https://media.bandwidth.com/x.jpg") == ("u", "p")
    assert legacy.media_auth("https://bandwidth.com.evil.net/x.jpg") is None
