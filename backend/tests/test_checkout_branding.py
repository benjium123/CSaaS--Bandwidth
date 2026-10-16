"""Stripe Checkout says "Ringlite", not the shared Stripe account's legal name.

The account (Apex Path LLC) sells for several products, so its public name cannot be changed;
every Ringlite Checkout Session carries its own branding_settings instead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import number_purchases, stripe_client
from tests.conftest import make_settings
from tests.test_workspace_plans import FakeStripe, _org

BRAND = {"display_name": "Ringlite", "button_color": "#3B76E8"}


async def test_the_plan_and_numbers_checkout_is_branded_ringlite(session, monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    monkeypatch.setattr(number_purchases, "require_carrier_funds", AsyncMock())
    org = await _org(session)
    settings = make_settings(stripe_webhook_secret="whsec_test")
    await number_purchases.create(session, settings, org.id, ["+12125550101"], plan_code="solo")
    assert fake.checkout_calls[-1]["branding_settings"] == BRAND


async def test_a_credit_top_up_checkout_is_branded_ringlite(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        checkout=SimpleNamespace(
            Session=SimpleNamespace(
                create=lambda **kw: calls.append(kw) or {"id": "cs_1", "url": "https://x"}
            )
        )
    )
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    org = SimpleNamespace(id=uuid.uuid4(), name="Org")
    await stripe_client.create_checkout_session(
        make_settings(stripe_secret_key="sk_test_x"),
        org=org,
        amount_micros=25_000_000,
        success_url="https://ringlite.io/ok",
        cancel_url="https://ringlite.io/no",
    )
    assert calls[-1]["branding_settings"] == BRAND


def test_branding_can_be_switched_off():
    assert (
        stripe_client.checkout_branding(
            make_settings(stripe_checkout_display_name="", stripe_checkout_button_color="")
        )
        == {}
    )
    assert stripe_client.checkout_branding(make_settings()) == {"branding_settings": BRAND}
