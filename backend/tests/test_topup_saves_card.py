"""A paid Checkout top-up keeps its card and, on the first top-up, switches auto-recharge on
(below $5 -> $10). Refused cards are not saved; a later top-up never re-enables it."""

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, PaymentMethod
from app.services import ban_list, payments, stripe_client
from tests.test_p24_stripe import _make_org


def _event(intent_id, *, pm="pm_new", customer="cus_new", org_id, source=None, amount=1000):
    metadata = {"org_id": str(org_id), "kind": "credit_topup"}
    if source:
        metadata["source"] = source
    return {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id,
                "amount_received": amount,
                "metadata": metadata,
                "payment_method": pm,
                "customer": customer,
            }
        },
    }


def _stripe(monkeypatch, fingerprint="fp_ok"):
    detached: list[str] = []

    async def retrieve(settings, pm_id):  # noqa: ANN001
        return {"id": pm_id, "brand": "visa", "last4": "4242", "fingerprint": fingerprint,
                "country": "US"}

    async def detach(settings, *, payment_method_id):  # noqa: ANN001
        detached.append(payment_method_id)

    async def not_risky(session, settings, intent):  # noqa: ANN001
        return False

    monkeypatch.setattr(stripe_client, "retrieve_payment_method", retrieve)
    monkeypatch.setattr(stripe_client, "detach_payment_method", detach)
    monkeypatch.setattr(payments, "refuse_risky_payment", not_risky)
    return detached


async def _post(client, monkeypatch, event):  # noqa: ANN001
    monkeypatch.setattr(stripe_client, "verify_webhook", lambda settings, payload, sig: event)
    r = await client.post(
        "/api/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,sig=1"}
    )
    assert r.status_code in (200, 204), r.text


async def _cards(session, org_id):
    set_org_context(session, org_id)
    return (
        await session.execute(sa.select(PaymentMethod).where(PaymentMethod.org_id == org_id))
    ).scalars().all()


async def test_first_topup_saves_the_card_and_turns_auto_recharge_on(client, session, monkeypatch):
    _stripe(monkeypatch)
    org = await _make_org(session, auto={})
    org_id = org.id
    await _post(client, monkeypatch, _event("pi_first", org_id=org_id))

    cards = await _cards(session, org_id)
    assert [(c.stripe_payment_method_id, c.stripe_customer_id, c.is_default) for c in cards] == [
        ("pm_new", "cus_new", True)
    ]
    card_id = str(cards[0].id)
    session.expire_all()
    fresh = await session.get(Org, org_id)
    assert fresh.credit_auto_recharge == {
        "enabled": True,
        "threshold_micros": 5_000_000,
        "amount_micros": 10_000_000,
        "payment_method_id": card_id,
    }

    # A replay changes nothing.
    await _post(client, monkeypatch, _event("pi_first", org_id=org_id))
    assert len(await _cards(session, org_id)) == 1


async def test_a_later_topup_never_switches_it_back_on(client, session, monkeypatch):
    _stripe(monkeypatch)
    org = await _make_org(session, auto={})
    org_id = org.id
    await _post(client, monkeypatch, _event("pi_one", org_id=org_id))
    # The customer turns it off (the PATCH stores None when nothing else is kept).
    session.expire_all()
    fresh = await session.get(Org, org_id)
    fresh.credit_auto_recharge = None
    await session.commit()

    await _post(client, monkeypatch, _event("pi_two", org_id=org_id, pm="pm_other"))
    session.expire_all()
    fresh = await session.get(Org, org_id)
    assert fresh.credit_auto_recharge is None
    # The second card is kept, not made default.
    cards = {c.stripe_payment_method_id: c.is_default for c in await _cards(session, org_id)}
    assert cards == {"pm_new": True, "pm_other": False}


async def test_a_banned_card_is_detached_and_not_saved(client, session, monkeypatch):
    detached = _stripe(monkeypatch, fingerprint="fp_banned")

    async def banned(session, identifiers):  # noqa: ANN001
        return True

    monkeypatch.setattr(ban_list, "matches", banned)
    org = await _make_org(session, auto={})
    org_id = org.id
    await _post(client, monkeypatch, _event("pi_ban", org_id=org_id))
    assert await _cards(session, org_id) == []
    assert detached == ["pm_new"]
    session.expire_all()
    fresh = await session.get(Org, org_id)
    assert not (fresh.credit_auto_recharge or {}).get("enabled")


async def test_an_auto_recharge_payment_does_not_save_a_card(client, session, monkeypatch):
    _stripe(monkeypatch)
    org = await _make_org(session, auto={})
    org_id = org.id
    await _post(client, monkeypatch, _event("pi_auto", org_id=org_id, source="auto_recharge"))
    assert await _cards(session, org_id) == []


async def test_existing_auto_recharge_settings_are_kept(client, session, monkeypatch):
    _stripe(monkeypatch)
    mine = {"enabled": True, "threshold_micros": 20_000_000, "amount_micros": 50_000_000,
            "payment_method_id": str(uuid.uuid4())}
    org = await _make_org(session, auto=dict(mine))
    org_id = org.id
    await _post(client, monkeypatch, _event("pi_keep", org_id=org_id))
    session.expire_all()
    fresh = await session.get(Org, org_id)
    assert fresh.credit_auto_recharge == mine
