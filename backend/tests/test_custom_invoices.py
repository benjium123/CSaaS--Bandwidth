# ruff: noqa: F811 - pytest fixtures imported from test_ops_console
"""Custom invoices: an operator bills a workspace's card for packages, balance credit and
custom items (with a discount line); what it contains is granted only once it is paid."""

from __future__ import annotations

import types

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import BillingPayment, PaymentMethod
from app.services import bundles, console, credits, custom_invoices, stripe_client
from tests.conftest import auth_headers
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401

CENT = 10_000


class _CardError(Exception):
    code = "card_declined"
    user_message = "Your card was declined."


class _FakeStripe:
    def __init__(self, *, decline: int = 0):
        self.decline = decline  # how many pay attempts fail before one succeeds
        self.items: list[dict] = []
        self.created: list[dict] = []
        self.voided: list[str] = []
        self.Invoice = types.SimpleNamespace(
            create=self._create,
            finalize_invoice=self._finalize,
            pay=self._pay,
            void_invoice=self._void,
        )
        self.InvoiceItem = types.SimpleNamespace(create=self._item)

    def _create(self, **kw):
        self.created.append(kw)
        return {"id": "in_1", "status": "draft", "metadata": kw["metadata"]}

    def _item(self, **kw):
        self.items.append(kw)
        return {"id": f"ii_{len(self.items)}"}

    def _total(self):
        return sum(i["amount"] for i in self.items)

    def _invoice(self, status):
        return {
            "id": "in_1",
            "number": "RL-0001",
            "status": status,
            "amount_paid": self._total() if status == "paid" else 0,
            "metadata": self.created[0]["metadata"],
            "hosted_invoice_url": "https://pay.example/in_1",
            "status_transitions": {"paid_at": 1_790_000_000 if status == "paid" else None},
            "payment_intent": "pi_inv_1" if status == "paid" else None,
        }

    def _finalize(self, invoice_id):
        return self._invoice("open")

    def _pay(self, invoice_id):
        if self.decline > 0:
            self.decline -= 1
            raise _CardError()
        return self._invoice("paid")

    def _void(self, invoice_id):
        self.voided.append(invoice_id)
        return self._invoice("void")


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = _FakeStripe()

    async def run_sync(fn, *args, **kwargs):
        kwargs.pop("idempotency_key", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: fake)
    monkeypatch.setattr(stripe_client, "_run_sync", run_sync)
    return fake


async def _with_card(session, org_id):
    set_org_context(session, org_id)
    session.add(
        PaymentMethod(
            org_id=org_id,
            stripe_customer_id="cus_1",
            stripe_payment_method_id="pm_1",
            brand="visa",
            last4="4242",
            is_default=True,
        )
    )
    await session.commit()


LINES = [
    {"type": "package", "package": "sms", "quantity": 2, "amount_cents": 3000},
    {"type": "credit", "amount_cents": 5000},
    {"type": "item", "description": "Onboarding", "amount_cents": 2000},
    {"type": "discount", "percent": 10},
]


async def test_price_lines(session):
    lines = await custom_invoices.price_lines(session, LINES)
    assert [ln["amount_cents"] for ln in lines] == [3000, 5000, 2000, -1000]
    assert lines[0]["units"] == 2 * bundles.UNITS_PER_BUNDLE["sms"]
    assert custom_invoices.totals(lines) == {
        "list": 10_000 * CENT, "discount": 1000 * CENT, "paid": 9000 * CENT
    }
    # A package without an amount is priced at its list price.
    default = await custom_invoices.price_lines(
        session, [{"type": "package", "package": "mms", "quantity": 3}]
    )
    assert default[0]["amount_cents"] == await bundles.bundle_list_price(session, "mms") * 3 // CENT
    for bad in (
        [{"type": "discount", "percent": 10}],
        [{"type": "item", "amount_cents": 100}],
        [{"type": "credit", "amount_cents": 0}],
        [{"type": "package", "package": "fax", "quantity": 1}],
        [{"type": "item", "description": "x", "amount_cents": 100},
         {"type": "discount", "amount_cents": 101}],
    ):
        with pytest.raises(ValidationFailedError):
            await custom_invoices.price_lines(session, bad)


async def test_invoice_is_charged_and_granted_once(ops, session, fake_stripe):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Invoice Org")
    await _with_card(session, org_id)

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices",
        json={"lines": LINES, "memo": "October package"},
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "paid"
    assert body["total_micros"] == 9000 * CENT
    assert body["number"] == "RL-0001"
    # The Stripe invoice carries every line, the discount as a negative item.
    assert [i["amount"] for i in fake_stripe.items] == [3000, 5000, 2000, -1000]
    assert fake_stripe.created[0]["collection_method"] == "charge_automatically"
    assert fake_stripe.created[0]["default_payment_method"] == "pm_1"

    session.expire_all()
    assert await bundles.units(session, org_id, "sms") == 2 * bundles.UNITS_PER_BUNDLE["sms"]
    assert await credits.balance(session, org_id) == 5000 * CENT

    # The webhook arriving after the synchronous payment grants nothing twice.
    event = {"type": "invoice.paid", "data": {"object": fake_stripe._invoice("paid")}}
    assert await custom_invoices.handle_event(session, event) is True
    session.expire_all()
    assert await bundles.units(session, org_id, "sms") == 2 * bundles.UNITS_PER_BUNDLE["sms"]
    assert await credits.balance(session, org_id) == 5000 * CENT

    row = (
        await session.execute(
            sa.select(BillingPayment).where(BillingPayment.org_id == org_id)
        )
    ).scalar_one()
    assert (row.kind, row.state) == ("invoice", "paid")
    assert (row.paid_micros, row.discount_micros, row.list_micros) == (
        9000 * CENT, 1000 * CENT, 10_000 * CENT
    )
    assert row.stripe_checkout_id == "in_1"

    # P&L: revenue and the discount given.
    table = await console.orgs_table(session, None, None)
    m = next(o for o in table["orgs"] if o["org_id"] == str(org_id))["metrics"]
    assert m["paid_invoice"] == 9000 * CENT
    assert m["discount"] >= 1000 * CENT

    # The customer's billing page lists it.
    listed = await custom_invoices.for_org(session, org_id)
    assert listed[0]["state"] == "paid" and listed[0]["memo"] == "October package"


async def test_declined_card_grants_nothing_until_a_retry_pays(ops, session, fake_stripe):
    fake_stripe.decline = 1
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Declined Org")
    await _with_card(session, org_id)

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices",
        json={"lines": LINES[:2]},
        headers=auth_headers(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "failed"
    assert r.json()["error"] == "Your card was declined."
    session.expire_all()
    assert await bundles.units(session, org_id, "sms") == 0
    assert await credits.balance(session, org_id) == 0

    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices/{r.json()['id']}/retry",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "paid"
    assert r.json()["error"] is None
    session.expire_all()
    assert await credits.balance(session, org_id) == 5000 * CENT


async def test_unpaid_invoice_can_be_voided(ops, session, fake_stripe):
    fake_stripe.decline = 1
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Void Org")
    await _with_card(session, org_id)
    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices",
        json={"lines": LINES[2:3]},
        headers=auth_headers(token),
    )
    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices/{r.json()['id']}/void",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "void"
    assert fake_stripe.voided == ["in_1"]


async def test_no_card_on_file_is_refused(ops, session, fake_stripe):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "No Card Org")
    r = await ops.post(
        f"/api/v1/ops/console/orgs/{org_id}/invoices",
        json={"lines": LINES[2:3]},
        headers=auth_headers(token),
    )
    assert r.status_code == 422, r.text
    assert "card on file" in r.text
    assert fake_stripe.created == []


async def test_other_invoices_are_not_ours(session):
    event = {"type": "invoice.paid", "data": {"object": {"id": "in_x", "metadata": {}}}}
    assert await custom_invoices.handle_event(session, event) is False
