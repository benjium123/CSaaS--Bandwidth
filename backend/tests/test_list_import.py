"""P11: contact-list import (services/list_import.py). DR-8/DR-9."""

from __future__ import annotations

import io
import uuid

import openpyxl
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.models import Contact, ContactList, ContactListRow
from app.services import list_import as list_import_svc
from tests.conftest import auth_headers, make_org_with_number

OUR = "+12145550100"
ALICE = "+12145550101"
BOB = "+12145550102"
ERIN = "+12145550199"  # will be put on the internal DNC list
FAY = "+12145550188"  # will opt out via the consent ledger

CSV_BYTES = (
    "name,phone,email,message\n"
    f"Alice,{ALICE},alice@example.com,Hi Alice\n"
    "Bob,214-555-0102,bob@example.com,\n"
    "Carol,not-a-phone,carol@example.com,Hi Carol\n"
    f"Dave,{ALICE},dave@example.com,Hi Dave\n"
    f"Erin,{ERIN},erin@example.com,Hi Erin\n"
    f"Fay,{FAY},fay@example.com,Hi Fay\n"
).encode()

MAPPING = {"phone": "phone", "first_name": "name", "email": "email", "message": "message"}


@pytest.fixture(autouse=True)
async def _drain_import_tasks():
    yield
    await list_import_svc.wait_for_pending_import_tasks()


async def _make_org(client) -> tuple[str, uuid.UUID]:
    token, org, _ = await make_org_with_number(client, "li1@example.com", "Org A", OUR)
    return token, uuid.UUID(org["id"])


async def _seed_list(session, org_id: uuid.UUID) -> ContactList:
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="Test list", source_filename="test.csv",
        status="importing",
    )
    session.add(lst)
    await session.commit()
    return lst


# ----------------------------------------------------------------------------------
# preview() - pure, no DB
# ----------------------------------------------------------------------------------
def test_preview_returns_headers_and_suggested_mapping():
    result = list_import_svc.preview("contacts.csv", CSV_BYTES)
    assert result["headers"] == ["name", "phone", "email", "message"]
    assert result["suggested_mapping"]["phone"] == "phone"
    assert result["suggested_mapping"]["message"] == "message"
    assert len(result["preview_rows"]) == 5  # capped at 5
    assert result["row_count"] == 6


def test_parse_upload_rejects_unknown_extension():
    with pytest.raises(ValidationFailedError):
        list_import_svc.parse_upload("contacts.txt", CSV_BYTES)


# ----------------------------------------------------------------------------------
# run_import() - CSV
# ----------------------------------------------------------------------------------
async def test_run_import_csv_reports_every_outcome(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client)
    h = auth_headers(token, str(org_id))

    await client.post("/api/v1/compliance/dnc", json={"e164": ERIN}, headers=h)
    await client.post("/api/v1/compliance/optout", json={"e164": FAY}, headers=h)

    async with get_sessionmaker()() as session:
        lst = await _seed_list(session, org_id)

    await list_import_svc.run_import(
        get_sessionmaker(),
        list_id=lst.id,
        org_id=org_id,
        filename="contacts.csv",
        data=CSV_BYTES,
        mapping=MAPPING,
    )

    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        refreshed = await session.get(ContactList, lst.id)
        assert refreshed.status == "ready"
        assert refreshed.total_rows == 6
        assert refreshed.accepted_count == 2  # Alice, Bob
        assert refreshed.invalid_count == 1  # Carol
        assert refreshed.duplicate_count == 1  # Dave (same phone as Alice)
        assert refreshed.dnc_count == 2  # Erin (dnc), Fay (opted_out)

        all_rows = list(
            (
                await session.execute(
                    sa.select(ContactListRow).where(ContactListRow.list_id == lst.id)
                )
            ).scalars().all()
        )
        by_name = {r.raw.get("name"): r for r in all_rows}

        alice = by_name["Alice"]
        assert alice.status == "accepted"
        assert alice.e164 == ALICE
        assert alice.contact_id is not None
        assert alice.fields["message"] == "Hi Alice"

        carol = by_name["Carol"]
        assert carol.status == "invalid"
        assert carol.reason

        dave = by_name["Dave"]
        assert dave.status == "duplicate"
        assert dave.e164 == ALICE

        erin = by_name["Erin"]
        assert erin.status == "dnc"
        assert erin.reason == "dnc"

        fay = by_name["Fay"]
        assert fay.status == "dnc"
        assert fay.reason == "opted_out"

        # Accepted rows upserted a Contact, matched on E.164.
        contact = (
            await session.execute(
                sa.select(Contact).where(Contact.id == alice.contact_id)
            )
        ).scalar_one()
        assert contact.first_name == "Alice"


async def test_run_import_xlsx(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["name", "phone"])
    sheet.append(["Gina", "+12145550177"])
    buf = io.BytesIO()
    workbook.save(buf)
    xlsx_bytes = buf.getvalue()

    async with get_sessionmaker()() as session:
        lst = await _seed_list(session, org_id)

    await list_import_svc.run_import(
        get_sessionmaker(),
        list_id=lst.id,
        org_id=org_id,
        filename="contacts.xlsx",
        data=xlsx_bytes,
        mapping={"phone": "phone", "first_name": "name"},
    )

    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        refreshed = await session.get(ContactList, lst.id)
        assert refreshed.status == "ready"
        assert refreshed.accepted_count == 1


async def test_run_import_requires_phone_in_mapping(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)

    async with get_sessionmaker()() as session:
        lst = await _seed_list(session, org_id)

    with pytest.raises(ValidationFailedError):
        await list_import_svc.run_import(
            get_sessionmaker(),
            list_id=lst.id,
            org_id=org_id,
            filename="contacts.csv",
            data=CSV_BYTES,
            mapping={"first_name": "name"},
        )


# ----------------------------------------------------------------------------------
# spawn_import() - the background-task test hook, and crash recording
# ----------------------------------------------------------------------------------
async def test_spawn_import_runs_in_background_and_completes(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)

    async with get_sessionmaker()() as session:
        lst = await _seed_list(session, org_id)

    list_import_svc.spawn_import(
        get_sessionmaker(),
        list_id=lst.id,
        org_id=org_id,
        filename="contacts.csv",
        data=CSV_BYTES,
        mapping=MAPPING,
    )
    await list_import_svc.wait_for_pending_import_tasks()

    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        refreshed = await session.get(ContactList, lst.id)
        assert refreshed.status == "ready"


async def test_spawn_import_crash_marks_list_failed(app_with_loopback):
    """A background failure (here: an invalid mapping) must never be lost silently - it is
    recorded on the list row so a client polling GET /outbound/lists/{id} can see it."""
    client, _carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)

    async with get_sessionmaker()() as session:
        lst = await _seed_list(session, org_id)

    list_import_svc.spawn_import(
        get_sessionmaker(),
        list_id=lst.id,
        org_id=org_id,
        filename="contacts.csv",
        data=CSV_BYTES,
        mapping={"first_name": "name"},  # missing "phone" -> run_import raises
    )
    await list_import_svc.wait_for_pending_import_tasks()

    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        refreshed = await session.get(ContactList, lst.id)
        assert refreshed.status == "failed"
        assert refreshed.error


# ----------------------------------------------------------------------------------
# Upload route guardrails (unbounded-upload fix)
# ----------------------------------------------------------------------------------
async def test_upload_rejects_oversized_file(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client)
    h = auth_headers(token, str(org_id))

    oversized = b"x" * (list_import_svc.MAX_LIST_BYTES + 1)
    r = await client.post(
        "/api/v1/outbound/lists",
        files={"file": ("big.csv", oversized, "text/csv")},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"


async def test_upload_rejects_unsupported_extension_before_reading_body(app_with_loopback):
    client, _carrier, _app = app_with_loopback
    token, org_id = await _make_org(client)
    h = auth_headers(token, str(org_id))

    r = await client.post(
        "/api/v1/outbound/lists",
        files={"file": ("contacts.txt", b"phone\n+12145550101\n", "text/plain")},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"
