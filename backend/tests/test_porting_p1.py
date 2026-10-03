"""P1: portability at submit, a carrier-free customer view, port-in notifications,
fix-and-resubmit / cancel, and the texting campaign a ported number lands on.

The Telnyx API is faked exactly as tests/test_p44f_porting.py does it - an httpx
MockTransport behind a carrier stand-in - so nothing here touches the network.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes.porting import _for_customer, _org_port, _public
from app.db.base import set_org_context
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import Notification, Org, OrgNumber, PortRequest, User
from app.repositories import users as users_repo
from app.services import billing_alerts, mailer, porting, tendlc
from app.storage.base import InMemoryObjectStore
from tests.test_p44f_porting import FORM, PDF, FakeTelnyx, _settings, _verified_org

NUMBER = "+12145550199"
RAW_NUMBER = "(214) 555-0199"
#: The carrier commits a date as a timestamp; the customer reads "Wed Oct 14".
FOC_TIMESTAMP = "2026-10-14T14:00:00Z"

ACCOUNT_SENTENCE = (
    "The account number doesn't match what your current provider has on file. Copy it "
    "exactly from your latest bill."
)
PIN_SENTENCE = (
    "The transfer PIN is wrong or missing. Ask your current provider for the port-out PIN."
)
NAME_SENTENCE = "The name doesn't match the account holder at your current provider."
ADDRESS_SENTENCE = "The service address doesn't match your current provider's records."
NOT_FOUND_SENTENCE = "Your current provider says these numbers aren't on that account."
DOC_SENTENCE = (
    "A document was rejected. Upload a clear, recent bill and a signed authorization letter."
)


class _Telnyx:
    """A scripted Telnyx porting / portability API.

    ``portability=None`` answers every requested number as portable; pass a list to answer
    exactly those rows instead (an omitted number reads as "not portable" to the caller).
    """

    def __init__(self, *, portability=None, order_status="in-process", order_details=None):
        self.calls: list[tuple[str, str]] = []
        self.portability = portability
        self.order_status = order_status
        self.order_details = order_details
        self.patch_status = 200
        self.patch_error = "The account number is incorrect"
        self.cancel_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/portability_checks"):
            if self.portability is None:
                asked = json.loads(request.content).get("phone_numbers") or []
                rows = [{"phone_number": n, "portable": True} for n in asked]
            else:
                rows = self.portability
            return httpx.Response(200, json={"data": rows})
        if path.endswith("/documents"):
            return httpx.Response(200, json={"data": {"id": f"doc-{len(self.calls)}"}})
        if path.endswith("/actions/confirm"):
            return httpx.Response(200, json={"data": {"id": "po-1"}})
        if path.endswith("/actions/cancel"):
            return httpx.Response(self.cancel_status, json={"data": {"id": "po-1"}})
        if request.method == "POST" and path.endswith("/porting_orders"):
            return httpx.Response(200, json={"data": [{"id": "po-1"}]})
        if request.method == "PATCH":
            if self.patch_status >= 400:
                return httpx.Response(
                    self.patch_status, json={"errors": [{"detail": self.patch_error}]}
                )
            return httpx.Response(200, json={"data": {"id": "po-1"}})
        if request.method == "GET" and "/porting_orders/" in path:
            status: dict = {"value": self.order_status}
            if self.order_details is not None:
                status["details"] = self.order_details
            data: dict = {"status": status}
            if self.order_status == "foc-date-confirmed":
                data["activation_settings"] = {"foc_datetime_actual": FOC_TIMESTAMP}
            return httpx.Response(200, json={"data": data})
        if request.method == "GET" and path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": [{"id": "tx-num-9"}]})
        return httpx.Response(404)

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))


@pytest.fixture(autouse=True)
def _clear_mailer():
    """mailer.outbox is process-wide; no test here may see another's mail."""
    mailer.outbox.clear()
    yield
    mailer.outbox.clear()


def _registry(handler: _Telnyx) -> dict:
    return {"telnyx": FakeTelnyx(handler)}


async def _make_user(session) -> User:
    user = await users_repo.create_user(
        session, email=f"p1-{uuid.uuid4().hex[:10]}@example.com", password="x" * 16
    )
    await session.commit()
    return user


async def _create(
    session,
    org,
    store,
    settings,
    *,
    registry=None,
    carrier="telnyx",
    numbers=None,
    form=None,
    user_id=None,
) -> PortRequest:
    return await porting.create_port_in(
        session,
        settings,
        store,
        org.id,
        user_id=user_id,
        carrier=carrier,
        numbers=numbers or [RAW_NUMBER],
        form={**FORM, **(form or {})},
        loa=PDF,
        invoice=PDF,
        registry=registry,
    )


async def _approve_to_submitted(session, settings, store, registry, port, user) -> PortRequest:
    port = await porting.approve(session, settings, store, registry, port, user.id)
    assert port.status == "submitted"
    assert port.details["orders"] == ["po-1"]
    return port


async def _bells(session, org_id: uuid.UUID) -> list[Notification]:
    set_org_context(session, org_id)
    return list(
        (
            await session.execute(
                sa.select(Notification).where(Notification.kind == "porting")
            )
        )
        .scalars()
        .all()
    )


async def _clear_bells(session, org_id: uuid.UUID) -> None:
    set_org_context(session, org_id)
    await session.execute(sa.delete(Notification))
    await session.commit()


def _text_of(message) -> str:
    """The plain-text part of a captured mailer message."""
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content()
    return ""


# ============================================================ A. portability at submit


async def test_a_non_portable_number_is_refused_at_submit(session):
    settings = _settings()
    org = await _verified_org(session)
    handler = _Telnyx(
        portability=[
            {
                "phone_number": NUMBER,
                "portable": False,
                "not_portable_reason": "This number is on an account with unpaid bills",
            }
        ]
    )
    with pytest.raises(ValidationFailedError) as err:
        await _create(session, org, InMemoryObjectStore(), settings, registry=_registry(handler))
    assert err.value.http_status == 422
    assert NUMBER in err.value.message
    assert "unpaid bills" in err.value.message
    assert handler.count("POST", "/portability_checks") == 1


async def test_a_number_the_check_does_not_mention_is_not_portable(session):
    settings = _settings()
    org = await _verified_org(session)
    handler = _Telnyx(portability=[])
    with pytest.raises(ValidationFailedError) as err:
        await _create(session, org, InMemoryObjectStore(), settings, registry=_registry(handler))
    assert NUMBER in err.value.message
    assert "not portable" in err.value.message


async def test_a_portable_number_is_accepted(session):
    settings = _settings()
    org = await _verified_org(session)
    handler = _Telnyx(portability=[{"phone_number": NUMBER, "portable": True}])
    port = await _create(
        session, org, InMemoryObjectStore(), settings, registry=_registry(handler)
    )
    assert port.status == "awaiting_review"
    assert port.numbers == [NUMBER]


async def test_signalwire_never_runs_the_portability_check(session):
    settings = _settings()
    org = await _verified_org(session)
    handler = _Telnyx(portability=[{"phone_number": NUMBER, "portable": False}])
    port = await _create(
        session,
        org,
        InMemoryObjectStore(),
        settings,
        registry=_registry(handler),
        carrier="signalwire",
    )
    assert port.status == "awaiting_review"
    assert handler.calls == []


async def test_without_a_registry_the_check_is_skipped(session):
    settings = _settings()
    org = await _verified_org(session)
    port = await _create(session, org, InMemoryObjectStore(), settings, registry=None)
    assert port.status == "awaiting_review"


# ====================================================== B. the customer never learns
# ======================================================    which carrier we use


def test_the_customer_view_hides_the_carrier_pin_and_documents():
    port = PortRequest(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        direction="in",
        carrier="telnyx",
        numbers=["+12145550100"],
        status="exception",
        details={
            "authorized_name": "Jane Doe",
            "business_name": "Sabine Property Group LLC",
            "account_number": "ACC-1",
            "billing_number": "+12145550100",
            "service_address": {"street": "100 Main St", "city": "Dallas"},
            "customer_reason": PIN_SENTENCE,
            "orders": ["po-1"],
        },
        events=[
            {"at": "2026-01-01T00:00:00Z", "text": "Filed with Telnyx (1 order(s))"},
            {
                "at": "2026-01-02T00:00:00Z",
                "text": "Approved - file it in the SignalWire dashboard (Phone Numbers > "
                "Port Requests)",
            },
        ],
        last_error="SignalWire refused the order: the account is on hold",
        loa_media_key="porting/x/loa.pdf",
        invoice_media_key="porting/x/invoice.pdf",
        secret_enc="gAAAAABm-encrypted-pin",
    )
    public = _public(port)
    assert public["carrier"] == "telnyx"  # the operator view keeps it

    payload = _for_customer(public)
    blob = json.dumps(payload).lower()
    assert "telnyx" not in blob
    assert "signalwire" not in blob
    assert "gAAAAA" not in blob
    assert "4321" not in blob
    assert "carrier" not in payload
    assert "secret_enc" not in payload
    assert "details" not in payload
    assert not any("pin" in key for key in payload)
    assert not any(key.endswith("media_key") for key in payload)

    assert payload["events"][0]["text"] == "Filed with the carrier (1 order(s))"
    assert payload["events"][1]["text"] == "Approved - filing with the carrier"
    assert payload["last_error"] == "the carrier refused the order: the account is on hold"

    assert payload["customer_reason"] == PIN_SENTENCE
    assert payload["authorized_name"] == "Jane Doe"
    assert payload["business_name"] == "Sabine Property Group LLC"
    assert payload["account_number"] == "ACC-1"
    assert payload["billing_number"] == "+12145550100"
    assert payload["service_address"]["street"] == "100 Main St"
    assert payload["can_edit"] is True
    assert payload["can_cancel"] is True


def test_a_ported_request_can_neither_be_edited_nor_cancelled():
    port = PortRequest(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        direction="in",
        carrier="telnyx",
        numbers=["+12145550100"],
        status="ported",
        details={},
        events=[],
    )
    payload = _for_customer(_public(port))
    assert payload["can_edit"] is False
    assert payload["can_cancel"] is False


async def test_a_real_requests_customer_payload_never_names_a_carrier(session):
    """The whole path - create, file, carrier complains - through the customer view."""
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx(order_status="exception")
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    handler.order_details = [{"description": "The transfer PIN is wrong or missing"}]
    assert await porting.poll_port_ins(session, settings, registry) == 1
    assert port.status == "exception"

    payload = _for_customer(_public(port))
    blob = json.dumps(payload).lower()
    assert "telnyx" not in blob
    assert "signalwire" not in blob
    assert payload["customer_reason"] == PIN_SENTENCE
    assert payload["last_error"] == "The transfer PIN is wrong or missing"
    assert "Filed with the carrier" in payload["events"][1]["text"]


# ================================================================= C. notifications


async def test_approving_sends_one_submitted_bell_per_recipient_and_no_email(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    await _clear_bells(session, org.id)

    port = await _approve_to_submitted(session, settings, store, registry, port, user)

    owners = {uid for uid, _ in await billing_alerts._recipients(session, org.id)}
    expected = {user.id} | owners
    bells = await _bells(session, org.id)
    assert {b.user_id for b in bells} == expected
    assert len(bells) == len(expected)
    assert all("was filed" in b.body for b in bells)
    assert mailer.outbox == []  # "submitted" is a bell, not an email


async def test_a_confirmed_date_is_told_once_by_bell_and_email(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    await _clear_bells(session, org.id)

    handler.order_status = "foc-date-confirmed"
    assert await porting.poll_port_ins(session, settings, registry) == 1

    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message["Subject"] == f"{settings.app_name}: port date confirmed"
    body = _text_of(message)
    expected_day = f"{datetime.fromisoformat('2026-10-14T14:00:00+00:00'):%a %b} 14"
    assert f"Your numbers move on {expected_day}." in body
    assert "Keep your old service active until then." in body
    assert body.strip().endswith("/settings/numbers")

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert "Your numbers move on" in bells[0].body

    # The sweeper reads the same order again: nobody is told twice.
    assert await porting.poll_port_ins(session, settings, registry) == 0
    assert len(mailer.outbox) == 1
    assert len(await _bells(session, org.id)) == 1


async def test_an_exception_carries_a_reason_the_customer_can_act_on(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    await _clear_bells(session, org.id)

    handler.order_status = "exception"
    handler.order_details = [
        {"code": "ACCOUNT_NUMBER", "description": "The account number is incorrect"}
    ]
    assert await porting.poll_port_ins(session, settings, registry) == 1

    assert port.status == "exception"
    assert port.last_error == "The account number is incorrect"
    assert port.details["customer_reason"] == ACCOUNT_SENTENCE

    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message["Subject"] == (
        f"{settings.app_name}: your number transfer needs attention"
    )
    assert ACCOUNT_SENTENCE in _text_of(message)

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert ACCOUNT_SENTENCE in bells[0].body


async def test_an_approval_the_carrier_refuses_notifies_the_customer(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    handler.patch_status = 500
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    await _clear_bells(session, org.id)

    port = await porting.approve(session, settings, store, registry, port, user.id)

    assert port.status == "exception"
    assert port.last_error == "The account number is incorrect"
    assert port.details["customer_reason"] == ACCOUNT_SENTENCE
    assert len(mailer.outbox) == 1
    assert "needs attention" in mailer.outbox[0]["Subject"]
    assert ACCOUNT_SENTENCE in _text_of(mailer.outbox[0])

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert ACCOUNT_SENTENCE in bells[0].body


async def test_rejecting_tells_the_customer_why(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings, user_id=user.id)
    await _clear_bells(session, org.id)

    port = await porting.reject(
        session, port, user.id, "The address does not match", settings=settings
    )

    assert port.status == "rejected"
    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message["Subject"] == f"{settings.app_name}: transfer request not accepted"
    assert "The address does not match" in _text_of(message)

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert "The address does not match" in bells[0].body


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The account number is incorrect", ACCOUNT_SENTENCE),
        ("Invalid PIN or passcode", PIN_SENTENCE),
        ("The name on the account does not match", NAME_SENTENCE),
        ("The service address does not match", ADDRESS_SENTENCE),
        ("The number was not found on this account", NOT_FOUND_SENTENCE),
        ("The invoice is unreadable", DOC_SENTENCE),
        (
            "Telnyx rejected the order",
            "Your current provider rejected the request: the carrier rejected the order",
        ),
    ],
)
def test_customer_reason_translates_the_carriers_words(raw, expected):
    assert porting.customer_reason(raw) == expected


# ================================================= E. fix-and-resubmit, cancel


async def test_an_awaiting_request_can_be_edited_in_place(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)

    updated = await porting.update_port_in(
        session,
        settings,
        store,
        None,
        port,
        form={**FORM, "account_number": "ACC-9", "service_street": "5 Oak Ave"},
        loa=None,
        invoice=None,
        user_id=None,
    )

    assert updated.status == "awaiting_review"
    assert updated.details["account_number"] == "ACC-9"
    assert updated.details["service_address"]["street"] == "5 Oak Ave"
    assert updated.numbers == [NUMBER]  # the numbers are never editable
    assert updated.events[-1]["text"] == "Details updated"


async def test_a_rejected_request_can_be_fixed_and_resubmitted(session):
    settings = _settings()
    org = await _verified_org(session)
    operator = await _make_user(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)
    await porting.reject(session, port, operator.id, "The address does not match")

    updated = await porting.update_port_in(
        session, settings, store, None, port, form=FORM, loa=None, invoice=None, user_id=None
    )

    assert updated.status == "awaiting_review"
    assert updated.last_error is None
    assert "customer_reason" not in (updated.details or {})
    assert not (updated.details or {}).get("resubmit")
    assert updated.events[-1]["text"] == "Resubmitted for review"


async def test_a_refused_request_asks_the_carrier_to_fix_the_order_it_already_has(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)
    set_org_context(session, org.id)
    port.status = "exception"
    port.last_error = "The transfer PIN is wrong"
    port.details = {**(port.details or {}), "customer_reason": PIN_SENTENCE, "orders": ["po-1"]}
    await session.commit()

    updated = await porting.update_port_in(
        session, settings, store, None, port, form=FORM, loa=None, invoice=None, user_id=None
    )

    assert updated.status == "awaiting_review"
    assert updated.details["resubmit"] is True
    assert updated.last_error is None
    assert "customer_reason" not in updated.details
    assert updated.events[-1]["text"] == "Resubmitted for review"


async def test_a_replacement_document_is_stored_under_a_new_key(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)
    old_loa = port.loa_media_key
    old_invoice = port.invoice_media_key

    updated = await porting.update_port_in(
        session,
        settings,
        store,
        None,
        port,
        form=FORM,
        loa=(b"%PDF-1.4 second", "application/pdf"),
        invoice=None,
        user_id=None,
    )

    assert updated.loa_media_key != old_loa
    assert "/loa-" in updated.loa_media_key
    assert updated.loa_media_key.endswith(".pdf")
    assert await store.exists(old_loa)  # what the operator reviewed is still there
    assert await store.exists(updated.loa_media_key)
    assert updated.invoice_media_key == old_invoice  # untouched without a new file


async def test_a_ported_request_cannot_be_edited(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)
    set_org_context(session, org.id)
    port.status = "ported"
    await session.commit()

    with pytest.raises(ConflictError):
        await porting.update_port_in(
            session, settings, store, None, port, form=FORM, loa=None, invoice=None, user_id=None
        )


async def test_resubmitting_patches_the_existing_order_and_files_nothing_new(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    assert handler.count("POST", "/porting_orders") == 1

    # The carrier asks for a fix; the customer posts the corrected details.
    set_org_context(session, org.id)
    port.status = "exception"
    port.last_error = "The account number is incorrect"
    port.details = {**(port.details or {}), "customer_reason": ACCOUNT_SENTENCE}
    await session.commit()
    port = await porting.update_port_in(
        session,
        settings,
        store,
        registry,
        port,
        form={**FORM, "account_number": "ACC-2", "pin": "9999"},
        loa=None,
        invoice=None,
        user_id=user.id,
    )
    assert port.details["resubmit"] is True

    handler.calls.clear()
    handler.order_status = "draft"
    port = await porting.approve(session, settings, store, registry, port, user.id)

    assert handler.count("POST", "/porting_orders") == 0  # nothing filed a second time
    assert handler.count("PATCH", "/porting_orders/po-1") == 1
    assert handler.count("GET", "/porting_orders/po-1") == 1
    assert handler.count("POST", "/actions/confirm") == 1
    assert port.status == "submitted"
    assert port.details["orders"] == ["po-1"]
    assert "resubmit" not in port.details


async def test_cancelling_before_filing_is_a_local_cancel(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    handler.calls.clear()
    await _clear_bells(session, org.id)

    cancelled = await porting.cancel_port_in(
        session, registry, port, user_id=user.id, settings=settings
    )

    assert cancelled.status == "cancelled"
    assert cancelled.events[-1]["text"] == "Cancelled by the customer"
    assert handler.count("POST", "/actions/cancel") == 0

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert "was cancelled" in bells[0].body
    assert mailer.outbox == []  # a cancellation is a bell, not an email


async def test_cancelling_a_filed_order_asks_the_carrier(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    handler.calls.clear()

    cancelled = await porting.cancel_port_in(
        session, registry, port, user_id=user.id, settings=settings
    )

    assert cancelled.status == "cancelled"
    assert handler.count("POST", "/porting_orders/po-1/actions/cancel") == 1


async def test_a_carrier_that_refuses_to_cancel_is_reported_without_naming_it(session):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    handler = _Telnyx()
    registry = _registry(handler)
    port = await _create(session, org, store, settings, registry=registry, user_id=user.id)
    port = await _approve_to_submitted(session, settings, store, registry, port, user)
    handler.cancel_status = 400

    with pytest.raises(ConflictError) as err:
        await porting.cancel_port_in(session, registry, port, user_id=user.id, settings=settings)

    assert "telnyx" not in err.value.message.lower()
    assert "signalwire" not in err.value.message.lower()
    assert port.status == "submitted"  # nothing changed locally either


async def test_cancelling_after_the_date_is_set_needs_support(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)
    set_org_context(session, org.id)
    port.status = "foc_confirmed"
    await session.commit()

    with pytest.raises(ConflictError) as err:
        await porting.cancel_port_in(session, None, port, user_id=None, settings=settings)
    assert "date is already set" in err.value.message


async def test_another_workspaces_port_request_is_not_found(session):
    settings = _settings()
    org = await _verified_org(session)
    store = InMemoryObjectStore()
    port = await _create(session, org, store, settings)

    other = Org(id=uuid.uuid4(), name="Other Org", slug=f"oo-{uuid.uuid4().hex[:16]}")
    session.add(other)
    await session.commit()

    with pytest.raises(NotFoundError):
        await _org_port(session, other.id, port.id)
    assert (await _org_port(session, org.id, port.id)).id == port.id


# ============================================ F. ported numbers get their campaign


async def test_ported_numbers_are_associated_and_flagged_when_they_still_need_one(
    session, monkeypatch
):
    settings = _settings()
    org = await _verified_org(session)
    user = await _make_user(session)
    store = InMemoryObjectStore()
    # A port filed by hand (SignalWire has no porting API), which is what the manual path
    # imports numbers from.
    port = await _create(
        session, org, store, settings, carrier="signalwire", user_id=user.id
    )
    port = await porting.approve(session, settings, store, None, port, user.id)
    assert port.status == "submitted" and port.details["manual"] is True

    associated: list[uuid.UUID] = []

    async def fake_associate(_session, _settings, org_id):
        associated.append(org_id)

    monkeypatch.setattr(tendlc, "associate_new_numbers", fake_associate)
    await _clear_bells(session, org.id)

    port = await porting.set_manual_status(
        session, None, port, "ported", foc_date=None, note="", settings=settings
    )

    assert port.status == "ported"
    assert associated == [org.id]

    set_org_context(session, org.id)
    numbers = (
        await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == NUMBER))
    ).scalars().all()
    assert len(numbers) == 1
    assert numbers[0].campaign_id is None  # nothing to associate it with yet

    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message["Subject"] == f"{settings.app_name}: your numbers are live"
    body = _text_of(message)
    assert f"{NUMBER} are now live on {settings.app_name}." in body
    assert "Lines page" in body
    assert body.strip().endswith("/settings/numbers")

    bells = await _bells(session, org.id)
    assert len(bells) == 1
    assert "Lines page" in bells[0].body


async def test_approve_retry_after_refused_filing_is_not_an_exception():
    """A first approve() refused by the carrier leaves last_error on an awaiting_review
    request; the retry that succeeds must end 'submitted', not 'exception'."""
    from app.services import porting as svc

    port = svc.PortRequest(direction="in", carrier="signalwire", numbers=["+15125550100"],
                           status="awaiting_review", details={}, events=[])
    port.last_error = "carrier refused the order"

    class _S:
        async def commit(self):
            return None

    out = await svc.approve(_S(), None, None, None, port, operator_id=None)
    assert out.status == "submitted"
    assert out.last_error is None
