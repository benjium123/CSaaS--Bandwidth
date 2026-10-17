# 10DLC Phase 1: Status Truth - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telnyx 10DLC status changes reach us at once by webhook. Every event, and the carrier's real decline reason, is stored and used, while the 5-minute poll stays as the safety net.

**Architecture:** A new `tendlc_events` table and a new `services/tendlc_events.py`
(parse / match / record). A new route `POST /api/v1/webhooks/telnyx/10dlc` treats each
webhook as a TRIGGER: it records the event and saves the reasons, then calls the existing
`tendlc.advance()` so the status comes from Telnyx's API, never from the payload. The
existing `_step()` rejection branches use the saved reason instead of generic text. Brand and
campaign filings send our webhook URL.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, pytest (asyncio, sqlite + postgres), httpx MockTransport.

**Spec:** `docs/superpowers/specs/2026-10-17-tendlc-lifecycle-design.md` (section 1, build order step 1).

## Global Constraints
- Work in `C:/Users/omer_/csaas_swg` on a branch made from `origin/main`. Never `git stash`, never `git checkout` other branches. Commit after each task.
- Run tests with `cd backend && python -m pytest -q <files> -p no:cacheprovider`. Lint with `ruff check <files>` (line length 100).
- The webhook payload is NEVER trusted for status. Status changes only through `tendlc.advance()` (which re-reads Telnyx).
- Reasons from an unverified webhook are stored with `verified=False` and are still shown. They are carrier text, not instructions.
- Migration id `0099_tendlc_events`, down_revision `0098_calendar_connections`. Additive only.
- The route always answers 204 for a well-formed body (also unmatched ids) and 400 for a non-JSON body. Telnyx retries on 5xx, and we never want a retry storm.
- Copy for customers: plain words, no carrier jargon in `detail` beyond the carrier's own reason text.
- Do not touch frontend files in this phase.

## Review Focus
1. **Webhook for an id we don't have** (another account on the shared Telnyx account, or a deleted brand). Expect 204, no row written, a log line `tendlc_event_unmatched`. Task 3 test.
2. **The same event delivered twice** (Telnyx retries). Expect one `tendlc_events` row, deduplicated on Telnyx's event `data.id`. Task 2 test.
3. **A rejection with no reasons, or reasons in `description` only** (MNO_REVIEW). Expect the customer text to fall back to the old generic message, or to use the description. Task 2 + Task 4 tests.
4. **A forged unsigned webhook claiming APPROVED.** Expect no stage change unless Telnyx's API says so. Task 3 test (`advance` sees a still-pending campaign).
5. **A number-assignment failure** in `associate_numbers`. Expect an event row with the error text, and the loop continues for the other numbers. Task 4 test.

---

### Task 1: Events table and saved rejection

**Files:**
- Create: `backend/app/models/tendlc_event.py`
- Modify: `backend/app/models/tendlc.py` (add `rejection` column)
- Modify: `backend/app/models/__init__.py` (export `TenDlcEvent`)
- Create: `backend/migrations/versions/0099_tendlc_events.py`
- Test: `backend/tests/test_tendlc_events.py`

**Interfaces:**
- Produces: `TenDlcEvent` (table `tendlc_events`), `TenDlcRegistration.rejection: dict | None`.

- [ ] **Step 1: Write the failing test** (`backend/tests/test_tendlc_events.py`)

```python
"""10DLC phase 1: the carrier's timeline and its decline reasons are stored."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, TenDlcEvent


async def _org(session) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Acme", slug=f"acme-{org_id.hex[:6]}"))
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def test_an_event_row_round_trips(session):
    org_id = await _org(session)
    session.add(
        TenDlcEvent(
            id=uuid.uuid4(),
            org_id=org_id,
            registration_id=None,
            subject="campaign",
            telnyx_id="C456",
            telnyx_event_id="evt-1",
            event_type="10dlc.campaign.update",
            payload_type="MNO_REVIEW",
            status="REJECTED",
            reasons=["Opt-in description is missing"],
            verified=False,
            raw={"data": {}},
            received_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    row = (await session.execute(sa.select(TenDlcEvent))).scalar_one()
    assert row.reasons == ["Opt-in description is missing"] and row.verified is False
```

- [ ] **Step 2: Run it.** `python -m pytest -q tests/test_tendlc_events.py -p no:cacheprovider`. Expected: ImportError `TenDlcEvent`.

- [ ] **Step 3: Model** (`backend/app/models/tendlc_event.py`)

```python
"""10DLC phase 1: every carrier event about a workspace's brand, campaign or numbers."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped
from app.db.types import GUID, PortableJSON


class TenDlcEvent(Base, TenantScoped):
    """One Telnyx 10DLC event (webhook) or one local carrier step we want on the timeline.

    ``verified`` is False when the webhook's ed25519 signature was missing or wrong: the row
    is kept (the reason text is useful), but nothing ever changes state on its word alone."""

    __tablename__ = "tendlc_events"
    __table_args__ = (
        sa.UniqueConstraint("telnyx_event_id", name="uq_tendlc_events_telnyx_event_id"),
        sa.Index("ix_tendlc_events_registration", "registration_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("tendlc_registrations.id", ondelete="CASCADE"), nullable=True
    )
    #: "brand" | "campaign" | "number"
    subject: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Telnyx brandId / campaignId, or the E.164 number.
    telnyx_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: Telnyx's event id (data.id); NULL for locally recorded steps. Dedupes retries.
    telnyx_event_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload_type: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    status: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    reasons: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    verified: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    raw: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
```

In `backend/app/models/tendlc.py`, add after `detail`:

```python
    #: Phase 1: the carrier's own words for the last refusal:
    #: {"subject": "brand"|"campaign", "reasons": [str], "verified": bool, "at": iso}.
    rejection: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
```

In `backend/app/models/__init__.py`, import and export `TenDlcEvent` next to `TenDlcRegistration`. Follow the file's existing pattern: an import line plus, if the file keeps an `__all__`, an entry there.

- [ ] **Step 4: Migration** (`backend/migrations/versions/0099_tendlc_events.py`)

```python
"""10DLC phase 1: carrier event timeline + the saved decline reason. Additive."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0099_tendlc_events"
down_revision = "0098_calendar_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tendlc_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "registration_id",
            GUID(),
            sa.ForeignKey("tendlc_registrations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(16), nullable=False),
        sa.Column("telnyx_id", sa.String(64), nullable=False),
        sa.Column("telnyx_event_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_type", sa.String(48), nullable=True),
        sa.Column("status", sa.String(48), nullable=True),
        sa.Column("reasons", PortableJSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw", PortableJSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telnyx_event_id", name="uq_tendlc_events_telnyx_event_id"),
    )
    op.create_index("ix_tendlc_events_org_id", "tendlc_events", ["org_id"])
    op.create_index(
        "ix_tendlc_events_registration", "tendlc_events", ["registration_id", "received_at"]
    )
    op.add_column("tendlc_registrations", sa.Column("rejection", PortableJSON(), nullable=True))


def downgrade():
    op.drop_column("tendlc_registrations", "rejection")
    op.drop_index("ix_tendlc_events_registration", table_name="tendlc_events")
    op.drop_index("ix_tendlc_events_org_id", table_name="tendlc_events")
    op.drop_table("tendlc_events")
```

Before writing it, open `0098_calendar_connections.py` and any recent migration that creates a `TenantScoped` table. If they add row-level-security statements (`op.execute("ALTER TABLE ... ENABLE ROW LEVEL SECURITY")` / policies), copy that block for `tendlc_events` exactly.

- [ ] **Step 5: Run.** `python -m pytest -q tests/test_tendlc_events.py tests/test_tendlc_self_serve.py -p no:cacheprovider`. Expected: all pass. Also run the repo's migration/model-parity test if one exists (`grep -rln "alembic" tests | head`), and run it.

- [ ] **Step 6: Commit.** `git add` the five files, then `git commit -m "feat(10dlc): tendlc_events timeline table and saved rejection reason (0099)"`.

---

### Task 2: Parse, record, dedupe (service)

**Files:**
- Create: `backend/app/services/tendlc_events.py`
- Test: `backend/tests/test_tendlc_events.py` (append)

**Interfaces:**
- Consumes: `TenDlcEvent`, `TenDlcRegistration.rejection` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class CarrierEvent: telnyx_event_id: str | None; event_type: str; subject: str; telnyx_id: str; payload_type: str | None; status: str | None; reasons: list[str]; raw: dict`
  - `parse(body: dict) -> CarrierEvent | None`: None when no usable id.
  - `async def record(session, *, org_id, registration_id, event: CarrierEvent, verified: bool) -> TenDlcEvent | None`: None when this `telnyx_event_id` is already stored.
  - `is_rejection(event: CarrierEvent) -> bool`
  - `rejection_text(rejection: dict | None, fallback: str) -> str`
  - `webhook_fields(settings) -> dict`: used in Task 5.

- [ ] **Step 1: Failing tests** (append to `tests/test_tendlc_events.py`)

```python
from app.services import tendlc_events


def _body(event_type, payload, event_id="evt-1"):
    return {"data": {"id": event_id, "event_type": event_type, "payload": payload}}


def test_parse_campaign_rejection_with_reasons():
    ev = tendlc_events.parse(_body("10dlc.campaign.update", {
        "campaignId": "C456", "type": "MNO_REVIEW", "status": "REJECTED",
        "reasons": [{"fields": ["messageFlow"], "description": "Opt-in is not described"}],
    }))
    assert (ev.subject, ev.telnyx_id, ev.payload_type, ev.status) == (
        "campaign", "C456", "MNO_REVIEW", "REJECTED")
    assert ev.reasons == ["Opt-in is not described"]
    assert tendlc_events.is_rejection(ev)


def test_parse_mno_description_only_and_brand_and_number():
    ev = tendlc_events.parse(_body("10dlc.campaign.update", {
        "campaignId": "C456", "type": "MNO_REVIEW", "status": "REJECTED",
        "description": "Sample messages do not match the use case"}))
    assert ev.reasons == ["Sample messages do not match the use case"]
    brand = tendlc_events.parse(_body("10dlc.brand.update", {
        "brandId": "B123", "type": "REGISTRATION", "status": "REGISTRATION_FAILED"}))
    assert (brand.subject, brand.telnyx_id) == ("brand", "B123")
    assert tendlc_events.is_rejection(brand)
    num = tendlc_events.parse(_body("10dlc.phone_number.update", {
        "phoneNumber": "+15125550133", "type": "ASSIGNMENT", "status": "FAILED",
        "reasons": [{"description": "Number already assigned"}]}))
    assert (num.subject, num.telnyx_id, num.reasons) == (
        "number", "+15125550133", ["Number already assigned"])


def test_parse_refuses_a_body_with_no_id():
    assert tendlc_events.parse({"data": {"event_type": "10dlc.campaign.update", "payload": {}}}) is None
    assert tendlc_events.parse({"nonsense": True}) is None


async def test_record_dedupes_telnyx_retries(session):
    org_id = await _org(session)
    ev = tendlc_events.parse(_body("10dlc.campaign.update", {
        "campaignId": "C456", "type": "TELNYX_REVIEW", "status": "ACCEPTED"}))
    first = await tendlc_events.record(session, org_id=org_id, registration_id=None, event=ev,
                                       verified=True)
    await session.commit()
    again = await tendlc_events.record(session, org_id=org_id, registration_id=None, event=ev,
                                       verified=True)
    assert first is not None and again is None
    assert len((await session.execute(sa.select(TenDlcEvent))).scalars().all()) == 1


def test_rejection_text_falls_back():
    assert tendlc_events.rejection_text(None, "Generic.") == "Generic."
    assert tendlc_events.rejection_text({"reasons": []}, "Generic.") == "Generic."
    assert tendlc_events.rejection_text(
        {"reasons": ["Opt-in is not described", "No privacy policy"]}, "Generic."
    ) == "The carrier said: Opt-in is not described; No privacy policy."
```

- [ ] **Step 2: Run.** Expected: ImportError `tendlc_events`.

- [ ] **Step 3: Implement** (`backend/app/services/tendlc_events.py`)

```python
"""10DLC phase 1: Telnyx 10DLC webhooks -> our timeline. Parsing and storage only.

The payload is never trusted for STATUS - tendlc.advance() re-reads Telnyx for that. What
we keep from it is the timeline and the carrier's reason text, which Telnyx's GET API does
not return. Event shape (developers.telnyx.com/docs/messaging/10dlc/event-notifications):
data {id, event_type: 10dlc.brand.update|10dlc.campaign.update|10dlc.phone_number.update,
payload {brandId|campaignId|phoneNumber, type, status, reasons[{fields, description}],
description}}."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models import TenDlcEvent

_SUBJECTS = {
    "10dlc.brand.update": ("brand", "brandId"),
    "10dlc.campaign.update": ("campaign", "campaignId"),
    "10dlc.phone_number.update": ("number", "phoneNumber"),
}
_REJECTED = frozenset({"REJECTED", "FAILED", "REGISTRATION_FAILED", "TCR_FAILED",
                       "TELNYX_FAILED", "MNO_REJECTED", "MNO_PROVISIONING_FAILED"})
WEBHOOK_PATH = "/api/v1/webhooks/telnyx/10dlc"


@dataclass(frozen=True)
class CarrierEvent:
    telnyx_event_id: str | None
    event_type: str
    subject: str
    telnyx_id: str
    payload_type: str | None
    status: str | None
    reasons: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _reasons(payload: dict) -> list[str]:
    out: list[str] = []
    for item in payload.get("reasons") or []:
        text = item.get("description") if isinstance(item, dict) else item
        if isinstance(text, str) and text.strip():
            out.append(text.strip()[:500])
    desc = payload.get("description")
    if not out and isinstance(desc, str) and desc.strip():
        out.append(desc.strip()[:500])
    return out


def parse(body: dict) -> CarrierEvent | None:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    event_type = str(data.get("event_type") or "")
    subject, id_key = _SUBJECTS.get(event_type, (None, None))
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    telnyx_id = str(payload.get(id_key) or "").strip() if id_key else ""
    if not subject or not telnyx_id:
        return None
    status = payload.get("status")
    return CarrierEvent(
        telnyx_event_id=str(data["id"])[:64] if data.get("id") else None,
        event_type=event_type[:64],
        subject=subject,
        telnyx_id=telnyx_id[:64],
        payload_type=str(payload.get("type"))[:48] if payload.get("type") else None,
        status=str(status).upper()[:48] if status else None,
        reasons=_reasons(payload),
        raw=body,
    )


def is_rejection(event: CarrierEvent) -> bool:
    return (event.status or "") in _REJECTED


async def record(session, *, org_id, registration_id, event: CarrierEvent,
                 verified: bool) -> TenDlcEvent | None:
    if event.telnyx_event_id:
        seen = (await session.execute(
            sa.select(TenDlcEvent.id).where(TenDlcEvent.telnyx_event_id == event.telnyx_event_id)
        )).scalar_one_or_none()
        if seen is not None:
            return None
    row = TenDlcEvent(
        id=uuid.uuid4(), org_id=org_id, registration_id=registration_id,
        subject=event.subject, telnyx_id=event.telnyx_id,
        telnyx_event_id=event.telnyx_event_id, event_type=event.event_type,
        payload_type=event.payload_type, status=event.status, reasons=list(event.reasons),
        verified=verified, raw=event.raw, received_at=datetime.now(timezone.utc),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:  # a concurrent retry won the unique constraint
        return None
    return row


def rejection_text(rejection: dict | None, fallback: str) -> str:
    reasons = [r for r in (rejection or {}).get("reasons") or [] if r]
    if not reasons:
        return fallback
    return "The carrier said: " + "; ".join(reasons) + "."


def webhook_fields(settings) -> dict:
    """Task 5: where Telnyx sends brand/campaign events. Empty unless we have a public
    https API base (local/dev never registers a webhook)."""
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    if not base.startswith("https://"):
        return {}
    url = base + WEBHOOK_PATH
    return {"webhookURL": url, "webhookFailoverURL": url}
```

- [ ] **Step 4: Run.** `python -m pytest -q tests/test_tendlc_events.py -p no:cacheprovider` passes. `ruff check app/services/tendlc_events.py tests/test_tendlc_events.py` is clean.

- [ ] **Step 5: Commit.** `feat(10dlc): parse and record Telnyx 10DLC events; reason text`

---

### Task 3: Webhook route and handler (trigger, never trust)

**Files:**
- Modify: `backend/app/services/tendlc.py` (add `handle_carrier_event`)
- Modify: `backend/app/api/routes/webhooks.py` (add route before `@router.post("/{carrier_name}/messaging")`)
- Test: `backend/tests/test_tendlc_webhook.py`

**Interfaces:**
- Consumes: `tendlc_events.parse/record/is_rejection`, `tendlc.advance(session, settings, reg_id, *, client=None)`, `_telnyx_ref(record)`, `providers.telnyx.webhooks.verify(headers, public_key_b64, raw_body) -> bool`, `settings.telnyx_public_key` (SecretStr).
- Produces: `async def handle_carrier_event(session, settings, event, *, verified: bool, client=None) -> str` returning `"unmatched" | "duplicate" | "recorded"`.

- [ ] **Step 1: Failing tests** (`backend/tests/test_tendlc_webhook.py`). Reuse the helpers of `tests/test_tendlc_self_serve.py` by importing them: `from tests.test_tendlc_self_serve import _workspace, _checkout, _pay, _stage, _settings, FakeTelnyx` plus its fixtures `stripe_stub` and `telnyx`. Re-export the fixtures with `from tests.test_tendlc_self_serve import stripe_stub, telnyx  # noqa: F401`.

```python
import pytest
import sqlalchemy as sa

from app.db.session import get_sessionmaker
from app.models import TenDlcEvent, TenDlcRegistration
from app.services import tendlc, tendlc_events
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout, _pay, _settings, _stage, _workspace, stripe_stub, telnyx,
)


def _ev(event_type, payload, event_id):
    return tendlc_events.parse({"data": {"id": event_id, "event_type": event_type,
                                         "payload": payload}})


async def _to_campaign_filed(session, telnyx):
    org, brand, campaign, number = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_filed"
    return reg_id, sm


async def test_unknown_id_is_unmatched_and_writes_nothing(session):
    ev = _ev("10dlc.campaign.update", {"campaignId": "NOPE", "status": "ACCEPTED"}, "e0")
    sm = get_sessionmaker()
    async with sm() as s:
        assert await tendlc.handle_carrier_event(s, _settings(), ev, verified=True) == "unmatched"
    async with sm() as s:
        assert (await s.execute(sa.select(sa.func.count(TenDlcEvent.id))
                .execution_options(allow_unscoped=True))).scalar_one() == 0


async def test_forged_approval_changes_nothing_while_telnyx_says_pending(
        session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    ev = _ev("10dlc.campaign.update",
             {"campaignId": "C456", "type": "MNO_REVIEW", "status": "ACCEPTED"}, "e1")
    async with sm() as s:
        out = await tendlc.handle_carrier_event(s, _settings(), ev, verified=False,
                                                client=telnyx.client)
    assert out == "recorded"
    assert await _stage(reg_id) == "campaign_filed"  # FakeTelnyx still says TCR_PENDING


async def test_rejection_reason_is_saved_and_the_status_rechecked(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    ev = _ev("10dlc.campaign.update", {"campaignId": "C456", "type": "MNO_REVIEW",
             "status": "REJECTED", "reasons": [{"description": "Opt-in is not described"}]},
             "e2")
    async with sm() as s:
        await tendlc.handle_carrier_event(s, _settings(), ev, verified=True, client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        reg = (await s.execute(sa.select(TenDlcRegistration).where(TenDlcRegistration.id == reg_id)
               .execution_options(allow_unscoped=True))).scalar_one()
        assert reg.rejection["reasons"] == ["Opt-in is not described"]
        assert "Opt-in is not described" in reg.detail


async def test_the_same_event_twice_is_one_row(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    ev = _ev("10dlc.campaign.update", {"campaignId": "C456", "status": "ACCEPTED"}, "e3")
    async with sm() as s:
        assert await tendlc.handle_carrier_event(s, _settings(), ev, verified=True,
                                                 client=telnyx.client) == "recorded"
    async with sm() as s:
        assert await tendlc.handle_carrier_event(s, _settings(), ev, verified=True,
                                                 client=telnyx.client) == "duplicate"


async def test_route_answers_204_and_400(client):
    r = await client.post("/api/v1/webhooks/telnyx/10dlc", content=b"not json")
    assert r.status_code == 400
    r = await client.post("/api/v1/webhooks/telnyx/10dlc",
                          json={"data": {"id": "x", "event_type": "10dlc.brand.update",
                                         "payload": {"brandId": "NOPE"}}})
    assert r.status_code == 204
```

Use the exact unscoped-query option the codebase uses: `ALLOW_UNSCOPED_KEY` from `app.db.base`, as `.execution_options(**{ALLOW_UNSCOPED_KEY: True})`. Replace `allow_unscoped=True` in the snippets above with that form.

- [ ] **Step 2: Run.** Expected: AttributeError `handle_carrier_event`; the route test gets 404.

- [ ] **Step 3: Implement `handle_carrier_event`** in `backend/app/services/tendlc.py` (after `advance`):

```python
async def handle_carrier_event(session, settings, event, *, verified: bool, client=None) -> str:
    """10DLC phase 1: a Telnyx webhook is a TRIGGER. Record it on the workspace's timeline,
    keep the carrier's reason text, then let advance() ask Telnyx itself what changed."""
    from app.services import tendlc_events

    rows = (
        await session.execute(
            sa.select(TenDlcRegistration, Brand, Campaign)
            .join(Brand, Brand.id == TenDlcRegistration.brand_id)
            .join(Campaign, Campaign.id == TenDlcRegistration.campaign_id)
            .where(TenDlcRegistration.stage.notin_(("cancelled", "expired")))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    reg = None
    if event.subject == "brand":
        reg = next((r for r, b, _ in rows if _telnyx_ref(b) == event.telnyx_id), None)
    elif event.subject == "campaign":
        reg = next((r for r, _, c in rows if _telnyx_ref(c) == event.telnyx_id), None)
    else:
        number = (
            await session.execute(
                sa.select(OrgNumber).where(OrgNumber.e164 == event.telnyx_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().first()
        if number is not None:
            reg = next((r for r, _, _ in rows if r.org_id == number.org_id
                        and r.stage == "active"), None)
    if reg is None:
        log.info("tendlc_event_unmatched", subject=event.subject, telnyx_id=event.telnyx_id)
        return "unmatched"
    reg_id, org_id = reg.id, reg.org_id
    set_org_context(session, org_id)
    row = await tendlc_events.record(
        session, org_id=org_id, registration_id=reg_id, event=event, verified=verified
    )
    if row is None:
        await session.commit()
        return "duplicate"
    if event.subject in ("brand", "campaign") and tendlc_events.is_rejection(event):
        reg.rejection = {
            "subject": event.subject,
            "reasons": list(event.reasons),
            "verified": verified,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    await session.commit()
    if reg.stage in WORKING_STAGES:
        await advance(session, settings, reg_id, client=client)
    return "recorded"
```

Check that `OrgNumber` has an `e164` column; it is imported at the top of `tendlc.py` and used in `associate_numbers`. If the column name differs, use the one `associate_numbers` uses.

- [ ] **Step 4: Route** in `backend/app/api/routes/webhooks.py`, placed directly above `@router.post("/{carrier_name}/messaging")`:

```python
@router.post("/telnyx/10dlc")
async def telnyx_tendlc_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """10DLC phase 1: brand / campaign / number status events. A trigger only - the status
    is re-read from Telnyx. Unsigned or badly signed events are kept as unverified."""
    import json

    from app.providers.telnyx import webhooks as telnyx_webhooks
    from app.services import tendlc, tendlc_events

    raw = await request.body()
    try:
        body = json.loads(raw)
    except ValueError:
        return Response(status_code=400)
    settings = request.app.state.settings
    verified = telnyx_webhooks.verify(
        request.headers, settings.telnyx_public_key.get_secret_value(), raw
    )
    event = tendlc_events.parse(body)
    if event is None:
        return Response(status_code=204)
    try:
        await tendlc.handle_carrier_event(session, settings, event, verified=verified)
    except Exception:  # never make Telnyx retry-storm us; the 5-min poll is the safety net
        log.exception("tendlc_webhook_failed", event_type=event.event_type)
    return Response(status_code=204)
```

Use the module's existing `log`, `Response`, `Request`, `Depends`, `AsyncSession` and `get_session` imports (they are already used by the `/stripe` route). Check that `json` isn't already imported at module top; if it is, drop the local import.

- [ ] **Step 5: Run.** `python -m pytest -q tests/test_tendlc_webhook.py tests/test_tendlc_events.py tests/test_tendlc_self_serve.py -p no:cacheprovider` passes, and ruff is clean.

- [ ] **Step 6: Commit.** `feat(10dlc): Telnyx 10DLC webhook route - records the event and reason, re-checks status`

---

### Task 4: Use the carrier's reason; record assignment failures

**Files:**
- Modify: `backend/app/services/tendlc.py`: the two `REJECTED` branches in `_step`, `_refund_unused` / `_cancel_monthly` callers' `detail`, and the `except` in `associate_numbers`.
- Test: `backend/tests/test_tendlc_webhook.py` (append)

**Interfaces:**
- Consumes: `tendlc_events.rejection_text`, `tendlc_events.record`, `CarrierEvent`.

- [ ] **Step 1: Failing tests** (append)

```python
async def test_poll_only_rejection_keeps_the_generic_words(session, stripe_stub, telnyx):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    telnyx.campaign_status = {"campaignStatus": "MNO_REJECTED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_rejected"
    async with sm() as s:
        reg = (await s.execute(sa.select(TenDlcRegistration).where(TenDlcRegistration.id == reg_id)
               .execution_options(**{ALLOW_UNSCOPED_KEY: True}))).scalar_one()
        assert reg.rejection is None
        assert reg.detail.startswith("The carriers did not approve this campaign")


async def test_a_failed_number_assignment_is_on_the_timeline(session, stripe_stub, telnyx,
                                                             monkeypatch):
    reg_id, sm = await _to_campaign_filed(session, telnyx)
    real = telnyx.handler

    def failing(request):
        if request.url.path.endswith("/10dlc/phone_number_campaigns"):
            import httpx
            return httpx.Response(400, json={"errors": [{"detail": "Number already assigned"}]})
        return real(request)

    telnyx.handler = failing  # see note below
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    async with sm() as s:
        rows = (await s.execute(sa.select(TenDlcEvent).where(TenDlcEvent.subject == "number")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True}))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "FAILED"
    assert rows[0].reasons and rows[0].registration_id == reg_id
```

Note: `MockTransport` captured `fake.handler` at fixture time. To make the swap take effect, give `FakeTelnyx` a flag instead. Add `self.fail_assignment = False` in `tests/test_tendlc_self_serve.py`'s `FakeTelnyx.__init__`, and in its `handler`, before the `/10dlc/phone_number_campaigns` branch:
`if path.endswith("/10dlc/phone_number_campaigns") and self.fail_assignment: return httpx.Response(400, json={"errors": [{"detail": "Number already assigned"}]})`.
The test then sets `telnyx.fail_assignment = True` and drops the `failing` wrapper. Import `ALLOW_UNSCOPED_KEY` from `app.db.base` and `TenDlcEvent` at the top of the test file.

- [ ] **Step 2: Run.** Expected: the timeline test fails (no number event).

- [ ] **Step 3: Implement.**
In `_step`, brand REJECTED branch, replace the generic `error=` / detail use with:

```python
        elif verdict == REJECTED:
            from app.services.tendlc_events import rejection_text

            why = rejection_text(reg.rejection, "The carrier did not accept this business.")
            advance_status(brand, "rejected", error=why)
            reg.stage = "brand_rejected"
            refund = await _refund_unused(settings, reg)
            reg.detail = refund if reg.rejection is None else f"{why} {refund}"
```

Campaign REJECTED branch:

```python
        elif verdict == REJECTED:
            from app.services.tendlc_events import rejection_text

            why = rejection_text(reg.rejection, "The carriers did not accept this campaign.")
            advance_status(campaign, "rejected", error=why)
            reg.stage = "campaign_rejected"
            stop = await _cancel_monthly(settings, reg)
            reg.detail = stop if reg.rejection is None else f"{why} {stop}"
```

In `associate_numbers`, inside the existing per-number `except` (after the rollback and `log.exception`), add:

```python
            from app.services import tendlc_events

            try:
                set_org_context(session, reg.org_id)
                await tendlc_events.record(
                    session,
                    org_id=reg.org_id,
                    registration_id=reg.id,
                    event=tendlc_events.CarrierEvent(
                        telnyx_event_id=None,
                        event_type="local.number.assignment",
                        subject="number",
                        telnyx_id=str(number.e164),
                        payload_type="ASSIGNMENT",
                        status="FAILED",
                        reasons=[str(exc)[:500] or type(exc).__name__],
                        raw={},
                    ),
                    verified=True,
                )
                await session.commit()
            except Exception:
                log.exception("tendlc_assignment_event_failed", registration_id=str(reg.id))
```

The `except` must bind the exception (`except Exception as exc:`). Use the loop variable name that `associate_numbers` actually uses for the number row (read the function first). If the rollback expires `reg`, read `reg.id` / `reg.org_id` into locals before the loop.

- [ ] **Step 4: Run** the three test files. All pass. Ruff is clean.

- [ ] **Step 5: Commit.** `feat(10dlc): show the carrier's decline reason; failed number assignments on the timeline`

---

### Task 5: Send our webhook URL when filing

**Files:**
- Modify: `backend/app/services/telnyx_brand_filing.py` (after `payload = build_brand_payload(...)`, ~line 200)
- Modify: `backend/app/services/telnyx_campaign_filing.py` (after `payload[REFERENCE_ID_FIELD] = reference_id`, ~line 361)
- Test: `backend/tests/test_tendlc_webhook.py` (append)

**Interfaces:**
- Consumes: `tendlc_events.webhook_fields(settings) -> dict`.

- [ ] **Step 1: Failing test** (append)

```python
async def test_filings_carry_our_webhook_url_on_https(session, stripe_stub, telnyx):
    import json

    settings = _settings().model_copy(update={"public_base_url": "https://api.ringlite.test"})
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    sm = get_sessionmaker()
    await tendlc.tick(sm, settings, client=telnyx.client)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, settings, client=telnyx.client)
    url = "https://api.ringlite.test/api/v1/webhooks/telnyx/10dlc"
    brand_body = json.loads(telnyx.posts("/10dlc/brand")[0].content)
    camp_body = json.loads(telnyx.posts("/10dlc/campaignBuilder")[0].content)
    assert brand_body["webhookURL"] == url and camp_body["webhookURL"] == url
    assert brand_body["webhookFailoverURL"] == url


def test_no_webhook_fields_without_https():
    assert tendlc_events.webhook_fields(_settings().model_copy(
        update={"public_base_url": "http://localhost:8000"})) == {}
```

If `Settings` is not a pydantic model with `model_copy`, build the settings with `tests.conftest.make_settings(public_base_url=...)` merged with what `_settings()` sets. Read `_settings()` in `test_tendlc_self_serve.py` first.

- [ ] **Step 2: Run.** Expected: KeyError `webhookURL`.

- [ ] **Step 3: Implement.** In both filing services, right after the payload is built:

```python
    from app.services.tendlc_events import webhook_fields

    payload.update(webhook_fields(settings))
```

In `telnyx_brand_filing.py`, `settings` is the function's settings parameter. Confirm its name in the function signature and use that name, not the resolved carrier settings.

- [ ] **Step 4: Run** the three test files plus `tests/test_tendlc_self_serve.py` and any `test_telnyx_brand_filing*` / `test_telnyx_campaign_filing*` tests (`ls tests | grep -i filing`). All pass. Ruff is clean.

- [ ] **Step 5: Commit.** `feat(10dlc): brand and campaign filings register our 10DLC webhook URL`

---

### Task 6: Release (controller, not a subagent)

- [ ] Run the full 10DLC + webhook related suite: `python -m pytest -q -k "tendlc or texting or 10dlc or webhook or registration" -p no:cacheprovider`. Compare failures against `origin/main` (CI is already red there). Zero NEW failures is the bar.
- [ ] Push to `main`. Wait for the GitHub "API image" run to succeed. Then `sh deploy/deploy-api.sh <sha>`; it runs `alembic upgrade head` (0099) and recreates only the api.
- [ ] Live check: `curl -s -o /dev/null -w '%{http_code}' -X POST https://ringlite.io/api/v1/webhooks/telnyx/10dlc -H 'content-type: application/json' -d '{"data":{"id":"probe","event_type":"10dlc.brand.update","payload":{"brandId":"NOPE"}}}'` returns 204, and the api log shows `tendlc_event_unmatched`.
- [ ] Verify the unverified Telnyx field names on the first real filing: `GET /10dlc/brand/{id}` shows `webhookURL`. If Telnyx ignores it, set the 10DLC webhook in the Telnyx portal to the same URL and note it in the spec.
