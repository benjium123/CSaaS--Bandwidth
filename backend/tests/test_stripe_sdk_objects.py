"""The REAL Stripe SDK, not a dict mock: responses and webhook events must reach callers as
plain dicts.

Since stripe-python 8 a StripeObject is not a dict and ``.get`` raises AttributeError.
Every other Stripe test mocks the SDK (or ``verify_webhook``) with plain dicts, so none of
them could see that - and the live webhook answered 500 to every Stripe delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
import stripe

from app.main import create_app
from app.services import stripe_client
from tests.conftest import make_settings

SECRET = "whsec_regression_test"


def _signed(payload: bytes, secret: str = SECRET) -> str:
    ts = int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {
            "id": f"evt_{int(time.time() * 1e6)}",
            "object": "event",
            "type": event_type,
            "data": {"object": obj},
        }
    ).encode()


def test_verified_webhook_event_is_a_plain_dict():
    payload = _event("checkout.session.expired", {"id": "cs_1", "metadata": {"a": "b"}})
    settings = make_settings(stripe_webhook_secret=SECRET)

    event = stripe_client.verify_webhook(settings, payload, _signed(payload))

    assert type(event) is dict
    assert type(event["data"]["object"]["metadata"]) is dict
    assert event.get("type") == "checkout.session.expired"


async def test_sdk_responses_come_back_as_plain_dicts_but_lists_stay_pageable():
    sub = stripe.Subscription.construct_from(
        {
            "id": "sub_1",
            "status": "active",
            "items": {"object": "list", "data": [{"price": {"id": "price_1"}, "quantity": 2}]},
        },
        "sk_test",
    )
    out = await stripe_client._run_sync(lambda: sub)
    assert type(out) is dict
    assert out.get("items", {}).get("data", [])[0].get("price", {}).get("id") == "price_1"

    listed = stripe.ListObject.construct_from({"object": "list", "data": []}, "sk_test")
    assert await stripe_client._run_sync(lambda: listed) is listed


@pytest.mark.parametrize(
    "event_type",
    ["checkout.session.expired", "checkout.session.completed", "customer.subscription.updated"],
)
async def test_live_shaped_delivery_for_another_product_is_acknowledged(engine, event_type):
    """The Stripe account also serves another product, whose events reach this endpoint.
    They must be acknowledged (2xx), or Stripe retries and eventually disables it."""
    app = create_app(make_settings(stripe_webhook_secret=SECRET))
    payload = _event(
        event_type,
        {"id": "cs_other", "object": "checkout.session", "mode": "payment", "metadata": {}},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": _signed(payload), "Content-Type": "application/json"},
        )
    assert r.status_code == 204, r.text


async def test_a_wrong_secret_is_still_refused(engine):
    app = create_app(make_settings(stripe_webhook_secret=SECRET))
    payload = _event("checkout.session.expired", {"id": "cs_x", "metadata": {}})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": _signed(payload, "whsec_other")},
        )
    assert r.status_code == 401, r.text
