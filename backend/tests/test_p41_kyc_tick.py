"""P41 background jobs: annual re-verification and Stripe Identity reconciliation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import KycPerson, KycProfile, Org
from app.services import kyc_tick, sanctions, stripe_client
from tests.conftest import make_settings

NOW = datetime(2027, 9, 20, 12, 0, tzinfo=timezone.utc)


async def _org_with_owner(
    session, *, status="approved", due=NOW - timedelta(days=1), verified_at=None
):
    org = Org(id=uuid.uuid4(), name="Tick Co", slug=f"tick-{uuid.uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    profile = KycProfile(
        id=uuid.uuid4(),
        org_id=org.id,
        status=status,
        legal_name="Tick Co LLC",
        next_reverification_at=due,
    )
    person = KycPerson(
        id=uuid.uuid4(),
        org_id=org.id,
        role="owner",
        full_name="Jane Smith",
        status="verified",
        verified_at=verified_at or (NOW - timedelta(days=400)),
        identity_hash="b" * 64,
    )
    session.add_all([profile, person])
    await session.commit()
    return org, profile, person


def _lists(tmp_path):
    settings = make_settings(kyc_enforced=True, security_data_dir=str(tmp_path))
    directory = sanctions.list_dir(settings)
    directory.mkdir(parents=True)
    (directory / "ofac_sdn.txt").write_text("SOMEONE ELSE ENTIRELY\n", encoding="utf-8")
    return settings


async def test_due_business_moves_to_reverification_and_back(session, tmp_path):
    settings = _lists(tmp_path)
    org, profile, person = await _org_with_owner(session)

    counts = await kyc_tick.reverification_tick(session, settings, now=NOW)
    assert counts["reverification_started"] == 1
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "reverification_due"

    # Still inside the grace period, nobody has re-verified: nothing changes.
    counts = await kyc_tick.reverification_tick(session, settings, now=NOW + timedelta(days=3))
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "reverification_due"

    # The owner redoes the ID check.
    person.verified_at = NOW + timedelta(days=4)
    await session.commit()
    counts = await kyc_tick.reverification_tick(session, settings, now=NOW + timedelta(days=5))
    assert counts["reverified"] == 1
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "approved"
    assert profile.next_reverification_at.replace(tzinfo=timezone.utc) > NOW + timedelta(days=360)


async def test_lapsed_reverification_stops_telephony(session, tmp_path):
    from app.services import telephony_access

    settings = _lists(tmp_path)
    org, profile, _ = await _org_with_owner(session, status="reverification_due")
    await kyc_tick.reverification_tick(
        session, settings, now=NOW + timedelta(days=settings.kyc_reverify_grace_days + 1)
    )
    set_org_context(session, org.id)
    await session.refresh(profile)
    assert profile.status == "needs_info"
    assert (
        await telephony_access.refusal(session, settings, org.id, "sms") == "account_not_verified"
    )


async def test_reconcile_picks_up_a_missed_webhook(session, monkeypatch):
    settings = make_settings(kyc_enforced=True, stripe_secret_key="sk_test_x")
    org = Org(id=uuid.uuid4(), name="Rec", slug=f"rec-{uuid.uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    person = KycPerson(
        id=uuid.uuid4(),
        org_id=org.id,
        role="owner",
        full_name="Jane Smith",
        status="pending",
        stripe_verification_session_id="vs_missed",
    )
    session.add(person)
    await session.commit()
    await session.execute(
        sa.update(KycPerson)
        .where(KycPerson.id == person.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await session.commit()

    async def fake_retrieve(s, vs_id):
        return {
            "id": vs_id,
            "status": "verified",
            "metadata": {},
            "first_name": "Jane",
            "last_name": "Smith",
            "dob": "1980-04-02",
            "document_type": "passport",
            "document_country": "CA",
            "error_code": None,
        }

    monkeypatch.setattr(stripe_client, "retrieve_verification_outcome", fake_retrieve)
    fixed = await kyc_tick.reconcile_identity_sessions(session, settings)
    assert fixed == 1
    set_org_context(session, org.id)
    await session.refresh(person)
    assert person.status == "verified" and person.document_country == "CA"
