"""Fax over Telnyx Programmable Fax (billing v2, B7). See app/services/fax.py."""

from __future__ import annotations

import io
import uuid

import httpx
import pytest
import sqlalchemy as sa
from pypdf import PdfWriter

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import Fax, OrgNumber
from app.services import fax as fax_svc
from app.services import telephony_billing
from app.storage.base import InMemoryObjectStore
from tests.conftest import make_settings
from tests.test_prepaid_telephony import _balance, _enable, _new_org, _usage

FAX_PAGE = 100_000  # $0.10/page


def _fax_settings(**overrides):
    return make_settings(
        telnyx_api_key="k",
        telnyx_fax_connection_id="fax-app-1",
        telnyx_voice_connection_id="voice-conn-1",
        **overrides,
    )


def _pdf2() -> bytes:
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


PDF2 = _pdf2()


async def _fax_number(session, org_id, *, fax_mode: bool = True, e164: str = "+12145550150") -> OrgNumber:
    set_org_context(session, org_id)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org_id,
        e164=e164,
        carrier="telnyx",
        number_type="local",
        status="active",
        is_active=True,
        provisioning={"fax_mode": True} if fax_mode else {},
    )
    session.add(number)
    await session.commit()
    return number


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ==================================================================================
# count_pages
# ==================================================================================
def test_count_pages_reads_a_pdf():
    assert fax_svc.count_pages(PDF2, "application/pdf") == 2


def test_count_pages_rejects_garbage():
    with pytest.raises(ValidationFailedError):
        fax_svc.count_pages(b"not a pdf at all", "application/pdf")


# ==================================================================================
# send
# ==================================================================================
async def test_send_happy_path_holds_money_and_reaches_the_carrier(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    await _fax_number(session, org.id)
    store = InMemoryObjectStore()
    settings = _fax_settings()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"data": {"id": "tx-fax-1"}})

    fax = await fax_svc.send(
        session,
        settings,
        store,
        org.id,
        from_e164="+12145550150",
        to_e164="+19725550000",
        data=PDF2,
        content_type="application/pdf",
        filename="doc.pdf",
        user_id=None,
        client=_client(handler),
    )

    assert fax.status == "sending"
    assert fax.provider_fax_id == "tx-fax-1"
    assert fax.page_count == 2
    assert await store.get(fax.media_key) == PDF2

    assert len(seen) == 1
    body = seen[0].content
    assert b"connection_id" in body
    assert b"fax-app-1" in body
    assert b"+12145550150" in body

    from app.services import credits

    set_org_context(session, org.id)
    assert await credits.outstanding_reserves(session, org.id) == 2 * FAX_PAGE


async def test_send_refused_on_low_balance_and_persists_nothing(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=150_000)  # < 200_000 for 2 pages
    await _fax_number(session, org.id)
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never reach the carrier")

    with pytest.raises(telephony_billing.TelephonyCreditsError):
        await fax_svc.send(
            session,
            settings,
            store,
            org.id,
            from_e164="+12145550150",
            to_e164="+19725550000",
            data=PDF2,
            content_type="application/pdf",
            filename="doc.pdf",
            user_id=None,
            client=_client(handler),
        )

    set_org_context(session, org.id)
    count = (await session.execute(sa.select(sa.func.count(Fax.id)))).scalar_one()
    assert count == 0


async def test_send_refused_when_number_is_not_in_fax_mode(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    await _fax_number(session, org.id, fax_mode=False)
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never reach the carrier")

    with pytest.raises(ValidationFailedError):
        await fax_svc.send(
            session,
            settings,
            store,
            org.id,
            from_e164="+12145550150",
            to_e164="+19725550000",
            data=PDF2,
            content_type="application/pdf",
            filename="doc.pdf",
            user_id=None,
            client=_client(handler),
        )


async def test_send_rejected_by_carrier_releases_the_hold(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    await _fax_number(session, org.id)
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": [{"detail": "bad number"}]})

    fax = await fax_svc.send(
        session,
        settings,
        store,
        org.id,
        from_e164="+12145550150",
        to_e164="+19725550000",
        data=PDF2,
        content_type="application/pdf",
        filename="doc.pdf",
        user_id=None,
        client=_client(handler),
    )

    assert fax.status == "failed"
    assert fax.failure_reason == "bad number"

    from app.services import credits

    set_org_context(session, org.id)
    assert await credits.outstanding_reserves(session, org.id) == 0
    assert await _balance(session, org.id) == 1_000_000


# ==================================================================================
# webhook: outbound
# ==================================================================================
async def _send_ok(session, org, store, settings, *, fax_id: str = "tx-fax-1") -> Fax:
    await _fax_number(session, org.id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"data": {"id": fax_id}})

    return await fax_svc.send(
        session,
        settings,
        store,
        org.id,
        from_e164="+12145550150",
        to_e164="+19725550000",
        data=PDF2,
        content_type="application/pdf",
        filename="doc.pdf",
        user_id=None,
        client=_client(handler),
    )


async def test_webhook_delivered_charges_once_and_releases_the_hold(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    store = InMemoryObjectStore()
    settings = _fax_settings()
    await _send_ok(session, org, store, settings)

    body = {
        "data": {
            "id": "ev-1",
            "event_type": "fax.delivered",
            "payload": {
                "fax_id": "tx-fax-1",
                "direction": "outbound",
                "page_count": 2,
                "status": "delivered",
            },
        }
    }

    outcome = await fax_svc.handle_webhook(session, settings, store, body)
    assert outcome == "fax.delivered"

    set_org_context(session, org.id)
    session.expunge_all()
    from app.services import credits

    fax = (
        await session.execute(sa.select(Fax).where(Fax.provider_fax_id == "tx-fax-1"))
    ).scalar_one()
    assert fax.status == "delivered"
    usage = await _usage(session, org.id)
    assert [(-row.amount_micros) for row in usage] == [2 * FAX_PAGE]
    assert await credits.outstanding_reserves(session, org.id) == 0
    assert await _balance(session, org.id) == 1_000_000 - 2 * FAX_PAGE

    # Re-sending the same event id is a no-op.
    outcome2 = await fax_svc.handle_webhook(session, settings, store, body)
    assert outcome2 == "duplicate"
    assert len(await _usage(session, org.id)) == 1


async def test_webhook_failed_releases_the_hold_and_charges_nothing(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    store = InMemoryObjectStore()
    settings = _fax_settings()
    await _send_ok(session, org, store, settings)

    body = {
        "data": {
            "id": "ev-2",
            "event_type": "fax.failed",
            "payload": {"fax_id": "tx-fax-1", "direction": "outbound"},
        }
    }
    outcome = await fax_svc.handle_webhook(session, settings, store, body)
    assert outcome == "fax.failed"

    from app.services import credits

    set_org_context(session, org.id)
    session.expunge_all()
    fax = (
        await session.execute(sa.select(Fax).where(Fax.provider_fax_id == "tx-fax-1"))
    ).scalar_one()
    assert fax.status == "failed"
    assert await _usage(session, org.id) == []
    assert await credits.outstanding_reserves(session, org.id) == 0


# ==================================================================================
# webhook: inbound
# ==================================================================================
async def test_webhook_received_stores_media_and_charges_inbound(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=0)
    await _fax_number(session, org.id)
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://media.example/fax.pdf"
        return httpx.Response(
            200, content=PDF2, headers={"content-type": "application/pdf"}
        )

    body = {
        "data": {
            "id": "ev-in",
            "event_type": "fax.received",
            "payload": {
                "fax_id": "tx-in-1",
                "direction": "inbound",
                "from": "+19725550000",
                "to": "+12145550150",
                "page_count": 2,
                "media_url": "https://media.example/fax.pdf",
            },
        }
    }

    outcome = await fax_svc.handle_webhook(session, settings, store, body, client=_client(handler))
    assert outcome == "received"

    set_org_context(session, org.id)
    session.expunge_all()
    fax = (
        await session.execute(sa.select(Fax).where(Fax.provider_fax_id == "tx-in-1"))
    ).scalar_one()
    assert fax.direction == "inbound"
    assert fax.status == "received"
    assert await store.get(fax.media_key) == PDF2
    assert await _balance(session, org.id) == -2 * FAX_PAGE


async def test_webhook_received_for_unknown_number_persists_nothing(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=0)
    # No OrgNumber at all for +12145550150.
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never fetch media for an unknown number")

    body = {
        "data": {
            "id": "ev-in2",
            "event_type": "fax.received",
            "payload": {
                "fax_id": "tx-in-2",
                "direction": "inbound",
                "from": "+19725550000",
                "to": "+12145550150",
                "page_count": 2,
                "media_url": "https://media.example/fax.pdf",
            },
        }
    }
    outcome = await fax_svc.handle_webhook(session, settings, store, body, client=_client(handler))
    assert outcome == "unknown_number"

    set_org_context(session, org.id)
    count = (
        await session.execute(
            sa.select(sa.func.count(Fax.id)).execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    assert count == 0


# ==================================================================================
# set_fax_mode
# ==================================================================================
async def test_set_fax_mode_enable_switches_connection_and_remembers_voice(session):
    org = await _new_org(session)
    number = await _fax_number(session, org.id, fax_mode=False)
    settings = _fax_settings()
    patches: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "pn1",
                            "tags": ["csaas"],
                            "connection_id": "voice-conn-1",
                            "phone_number": "+12145550150",
                        }
                    ]
                },
            )
        patches.append((str(request.url), __import__("json").loads(request.content)))
        return httpx.Response(200, json={"data": {}})

    updated = await fax_svc.set_fax_mode(session, settings, number, True, client=_client(handler))
    assert updated.provisioning["fax_mode"] is True
    assert updated.provisioning["voice_connection_before_fax"] == "voice-conn-1"
    assert len(patches) == 1
    assert patches[0][0].endswith("/v2/phone_numbers/pn1")
    assert patches[0][1] == {"connection_id": "fax-app-1"}


async def test_set_fax_mode_disable_restores_the_saved_voice_connection(session):
    org = await _new_org(session)
    number = await _fax_number(session, org.id, fax_mode=True)
    number.provisioning = {"fax_mode": True, "voice_connection_before_fax": "voice-conn-1"}
    await session.commit()
    settings = _fax_settings()
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "pn1",
                            "tags": ["csaas"],
                            "connection_id": "fax-app-1",
                            "phone_number": "+12145550150",
                        }
                    ]
                },
            )
        patches.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"data": {}})

    updated = await fax_svc.set_fax_mode(session, settings, number, False, client=_client(handler))
    assert updated.provisioning["fax_mode"] is False
    assert patches == [{"connection_id": "voice-conn-1"}]


async def test_set_fax_mode_refuses_a_number_not_csaas_owned(session):
    org = await _new_org(session)
    number = await _fax_number(session, org.id, fax_mode=False)
    settings = _fax_settings()
    patched = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patched
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "pn1",
                            "tags": ["crm"],
                            "connection_id": "voice-conn-1",
                            "phone_number": "+12145550150",
                        }
                    ]
                },
            )
        patched = True
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(ValidationFailedError):
        await fax_svc.set_fax_mode(session, settings, number, True, client=_client(handler))
    assert patched is False


# ==================================================================================
# Not-prepaid org
# ==================================================================================
async def test_not_prepaid_org_is_never_reserved_or_charged(session):
    org = await _new_org(session)
    org.telephony_prepaid = False  # gate off (model default is on since migration 0055)
    await session.commit()
    await _fax_number(session, org.id)
    store = InMemoryObjectStore()
    settings = _fax_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"data": {"id": "tx-fax-np"}})

    fax = await fax_svc.send(
        session,
        settings,
        store,
        org.id,
        from_e164="+12145550150",
        to_e164="+19725550000",
        data=PDF2,
        content_type="application/pdf",
        filename="doc.pdf",
        user_id=None,
        client=_client(handler),
    )
    assert fax.status == "sending"

    from app.services import credits

    set_org_context(session, org.id)
    assert await credits.outstanding_reserves(session, org.id) == 0
    assert await _balance(session, org.id) == 0

    body = {
        "data": {
            "id": "ev-np",
            "event_type": "fax.delivered",
            "payload": {
                "fax_id": "tx-fax-np",
                "direction": "outbound",
                "page_count": 2,
                "status": "delivered",
            },
        }
    }
    outcome = await fax_svc.handle_webhook(session, settings, store, body)
    assert outcome == "fax.delivered"
    assert await _usage(session, org.id) == []
    assert await _balance(session, org.id) == 0
