"""P9 human-facing appointment routes: GET/PATCH /api/v1/appointments. Appointments
themselves are booked by the AI worker (test_agent_tools.py covers that machine seam);
these tests cover the RBAC-gated, org-scoped human view + edit of the resulting rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import set_org_context
from app.models import Appointment
from tests.conftest import auth_headers, create_org, register_and_login


async def _seed_org(client, email: str, name: str) -> uuid.UUID:
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, uuid.UUID(org["id"])


async def _make_appointment(
    session,
    org_id: uuid.UUID,
    *,
    contact_e164: str = "+19725550199",
    status: str = "booked",
    created_at: datetime | None = None,
) -> Appointment:
    set_org_context(session, org_id)
    appt = Appointment(
        id=uuid.uuid4(),
        org_id=org_id,
        contact_e164=contact_e164,
        raw_when="tomorrow at 3",
        notes="",
        status=status,
        created_by="ai",
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(appt)
    await session.commit()
    return appt


async def test_list_appointments_newest_first(client, session):
    # F11c: explicit, strictly increasing created_at - SQLite's default-clock
    # resolution is not fine enough to guarantee two rows added back-to-back get
    # distinct timestamps, which would make "newest first" order-dependent on
    # insertion order rather than time (same fix as test_agent_tools.py's message
    # ordering test).
    token, org_id = await _seed_org(client, "appt1@example.com", "Org Appt 1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a1 = await _make_appointment(
        session, org_id, contact_e164="+19725550101", created_at=base
    )
    a2 = await _make_appointment(
        session, org_id, contact_e164="+19725550102", created_at=base + timedelta(seconds=1)
    )

    r = await client.get("/api/v1/appointments", headers=auth_headers(token, org_id))
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()]
    assert ids[:2] == [str(a2.id), str(a1.id)]


async def test_list_appointments_filters_by_status(client, session):
    token, org_id = await _seed_org(client, "appt2@example.com", "Org Appt 2")
    await _make_appointment(session, org_id, status="booked")
    await _make_appointment(session, org_id, status="canceled")

    r = await client.get(
        "/api/v1/appointments?status=canceled", headers=auth_headers(token, org_id)
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "canceled"


async def test_list_appointments_scoped_to_org(client, session):
    token_a, org_a = await _seed_org(client, "appt3a@example.com", "Org Appt 3A")
    _token_b, org_b = await _seed_org(client, "appt3b@example.com", "Org Appt 3B")
    await _make_appointment(session, org_a)
    await _make_appointment(session, org_b)

    r = await client.get("/api/v1/appointments", headers=auth_headers(token_a, org_a))
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


async def test_list_appointments_requires_membership(client):
    token = await register_and_login(client, "appt4@example.com")
    r = await client.get(
        "/api/v1/appointments", headers=auth_headers(token, uuid.uuid4())
    )
    assert r.status_code == 403


async def test_patch_appointment_updates_status(client, session):
    token, org_id = await _seed_org(client, "appt5@example.com", "Org Appt 5")
    appt = await _make_appointment(session, org_id)

    r = await client.patch(
        f"/api/v1/appointments/{appt.id}",
        json={"status": "done"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"

    r2 = await client.get("/api/v1/appointments", headers=auth_headers(token, org_id))
    assert r2.json()[0]["status"] == "done"


async def test_patch_appointment_updates_notes_and_scheduled_for(client, session):
    token, org_id = await _seed_org(client, "appt6@example.com", "Org Appt 6")
    appt = await _make_appointment(session, org_id)

    r = await client.patch(
        f"/api/v1/appointments/{appt.id}",
        json={"notes": "confirmed by phone", "scheduled_for": "2026-09-05T14:00:00+00:00"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes"] == "confirmed by phone"
    assert body["scheduled_for"].startswith("2026-09-05T14:00:00")


async def test_patch_appointment_bad_status_is_422(client, session):
    """F8: status is a closed vocabulary (booked/canceled/done) - anything else is a
    422 from pydantic validation, not silently written to the row."""
    token, org_id = await _seed_org(client, "appt9@example.com", "Org Appt 9")
    appt = await _make_appointment(session, org_id)

    r = await client.patch(
        f"/api/v1/appointments/{appt.id}",
        json={"status": "archived"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 422


async def test_patch_appointment_not_found_404(client, session):
    token, org_id = await _seed_org(client, "appt7@example.com", "Org Appt 7")

    r = await client.patch(
        f"/api/v1/appointments/{uuid.uuid4()}",
        json={"status": "done"},
        headers=auth_headers(token, org_id),
    )
    assert r.status_code == 404


async def test_patch_appointment_cross_org_is_404(client, session):
    """Appointment A belongs to org A; org B's admin cannot reach it at all - the
    TenantScoped read inside update_appointment resolves nothing for a foreign org."""
    token_a, org_a = await _seed_org(client, "appt8a@example.com", "Org Appt 8A")
    token_b, org_b = await _seed_org(client, "appt8b@example.com", "Org Appt 8B")
    appt = await _make_appointment(session, org_a)

    r = await client.patch(
        f"/api/v1/appointments/{appt.id}",
        json={"status": "done"},
        headers=auth_headers(token_b, org_b),
    )
    assert r.status_code == 404
