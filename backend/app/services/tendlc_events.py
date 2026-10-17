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
_REJECTED = frozenset(
    {
        "REJECTED",
        "FAILED",
        "REGISTRATION_FAILED",
        "TCR_FAILED",
        "TELNYX_FAILED",
        "MNO_REJECTED",
        "MNO_PROVISIONING_FAILED",
    }
)
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


async def record(
    session, *, org_id, registration_id, event: CarrierEvent, verified: bool
) -> TenDlcEvent | None:
    if event.telnyx_event_id:
        seen = (
            await session.execute(
                sa.select(TenDlcEvent.id).where(
                    TenDlcEvent.telnyx_event_id == event.telnyx_event_id
                )
            )
        ).scalar_one_or_none()
        if seen is not None:
            return None
    row = TenDlcEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        registration_id=registration_id,
        subject=event.subject,
        telnyx_id=event.telnyx_id,
        telnyx_event_id=event.telnyx_event_id,
        event_type=event.event_type,
        payload_type=event.payload_type,
        status=event.status,
        reasons=list(event.reasons),
        verified=verified,
        raw=event.raw,
        received_at=datetime.now(timezone.utc),
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
