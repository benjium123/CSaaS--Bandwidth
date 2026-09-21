"""Regression: a Telnyx order that carries a 10DLC ``campaign_id`` is refused BEFORE the
prepaid credit gate runs and BEFORE the carrier buys anything.

A Telnyx number's campaign association lives AT TELNYX (``services/telnyx_number_association``)
and has no local equivalent, so an order-time ``campaign_id`` could not be honoured: it would
be recorded as a local-only association the carrier never agreed to, and only discovered once
the number had already been bought and billed. ``routes/numbers.py`` therefore refuses the
order up front - after carrier resolution, but before the credit gate and before
``provider.order_number``.

As everywhere else in the numbers suite, the load-bearing assertions are the side-effect ones:
no charge, no purchase, no ``OrgNumber`` row. A 422 alone would not prove we stopped before
spending money.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.main import create_app
from app.models import OrgNumber
from app.providers.registry import CarrierRegistry
from app.services import telephony_billing
from tests.conftest import FakeCarrier, auth_headers, create_org, register_and_login

# Two of the suite's fictional 555-01XX numbers: one is seeded by hand (so the org
# demonstrably CAN store a number) and one is the number the rejected order asks for.
SEEDED_E164 = "+12145550100"
ORDER_E164 = "+12145550101"


class FakeTelnyxOrderCarrier:
    """Minimal stand-in for the Telnyx provisioning adapter.

    Only ``name`` matters to the guard under test - it is the same string
    ``telnyx_number_association.PROVIDER`` carries. The NumberProvider members exist so
    ``numbers_api.as_provider`` recognises the object on the paths that run AFTER the guard,
    and every provisioning call is recorded so a test can prove none of them happened.
    """

    name = "telnyx"

    def __init__(self) -> None:
        self.orders: list = []

    async def search_numbers(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("search_numbers must not be called")

    async def lookup_owned_number(self, e164, *args, **kwargs):
        """``None``, deliberately.

        Not True: that would re-attribute a hand-added number to telnyx in add_number's
        carrier fan-out (every test here seeds a number first). Not False: that would make
        add_number refuse the manual add outright. ``None`` leaves it on the primary carrier.
        """
        return None

    async def order_number(self, *args, **kwargs):
        self.orders.append(args[0] if args else None)
        raise AssertionError("provider.order_number must not be called for a rejected order")

    async def release_number(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("release_number must not be called")


@pytest.fixture
async def app_with_telnyx_provisioning(engine, webhook_settings):
    """``app_with_carrier``'s wiring plus a Telnyx-shaped provisioning carrier.

    Both carriers go into the ONE registry the app reads (the same ``state.carriers`` +
    ``state.carrier`` pair conftest._install() sets up): a registry that knew only about
    telnyx would send ``add_number``'s ownership check down the "cannot verify" branch and
    the seeded number could never be added.
    """
    application = create_app(webhook_settings)
    messaging = FakeCarrier()
    telnyx = FakeTelnyxOrderCarrier()
    application.state.carriers = CarrierRegistry(
        {messaging.name: messaging, telnyx.name: telnyx}, primary=messaging.name
    )
    application.state.carrier = messaging
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, telnyx


async def _persisted(session, e164: str) -> list[OrgNumber]:
    """Read rows back WITHOUT the org-scope filter.

    The request commits in its own session, and a scoped read here would report ``[]``
    whether or not the order persisted - which would make the negative assertion vacuous.
    """
    return list(
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.e164 == e164)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )


async def test_a_telnyx_order_with_a_campaign_id_is_refused_before_any_charge_or_purchase(
    app_with_telnyx_provisioning, session, monkeypatch
):
    client, telnyx = app_with_telnyx_provisioning
    token = await register_and_login(client, "telnyx-order-guard@example.com")
    org = await create_org(client, token, "Org Telnyx Guard")
    h = auth_headers(token, org["id"])

    seeded = await client.post("/api/v1/numbers", json={"e164": SEEDED_E164}, headers=h)
    assert seeded.status_code == 201, seeded.text

    gated: list = []
    charged: list = []

    async def _no_credit_gate(*args, **kwargs):
        gated.append((args, kwargs))

    async def _no_credit_charge(*args, **kwargs):
        charged.append((args, kwargs))

    # The route reaches both through the module object (`telephony_billing.<name>`), so
    # patching the module attributes intercepts its calls without touching production code.
    monkeypatch.setattr(telephony_billing, "require_number_credit", _no_credit_gate)
    monkeypatch.setattr(telephony_billing, "charge_new_number", _no_credit_charge)

    refused = await client.post(
        "/api/v1/numbers/order",
        json={
            # Non-null is all the guard looks at: a Telnyx campaign association can never
            # be recorded at order time, whatever campaign the id points at, so a synthetic
            # uuid keeps this test about the guard rather than about campaign state.
            "campaign_id": str(uuid.uuid4()),
            "carrier": "telnyx",
            "e164": ORDER_E164,
        },
        headers=h,
    )

    assert refused.status_code == 422, refused.text
    assert "telnyx" in refused.text.lower()
    assert "campaign" in refused.text.lower()

    assert telnyx.orders == [], "provider.order_number must never be called"
    assert gated == [], "the prepaid credit gate must not run for a rejected order"
    assert charged == [], "no number credit may be charged for a rejected order"

    # Nothing was stored: the hand-seeded number is present (proving the read works) and the
    # ordered one is not.
    assert len(await _persisted(session, SEEDED_E164)) == 1
    assert await _persisted(session, ORDER_E164) == []


async def test_a_telnyx_order_without_a_campaign_id_is_not_refused_by_the_guard(
    app_with_telnyx_provisioning,
):
    """The guard is keyed on ``campaign_id``, not on the carrier.

    Without one, the order falls through to the pre-existing checks - proven here by the
    duplicate-number pre-check, which itself runs BEFORE the credit gate and the purchase.
    """
    client, telnyx = app_with_telnyx_provisioning
    token = await register_and_login(client, "telnyx-no-campaign@example.com")
    org = await create_org(client, token, "Org Telnyx No Campaign")
    h = auth_headers(token, org["id"])
    seeded = await client.post("/api/v1/numbers", json={"e164": SEEDED_E164}, headers=h)
    assert seeded.status_code == 201, seeded.text

    r = await client.post(
        "/api/v1/numbers/order", json={"e164": SEEDED_E164, "carrier": "telnyx"}, headers=h
    )

    assert r.status_code == 409, r.text
    assert "already registered" in r.text.lower()
    assert telnyx.orders == []
