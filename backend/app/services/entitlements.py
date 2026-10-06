"""Per-workspace feature switches (P2). A missing row means the catalog default applies.

Reads must work for ANY org regardless of the session's current org context: we query with
``allow_unscoped`` and an explicit ``OrgFeature.org_id`` filter. Writes establish the org
context first, like every other service that writes tenant rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureDisabledError
from app.models.entitlements import OrgFeature
from app.models.org import Org

U = {ALLOW_UNSCOPED_KEY: True}


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    group: str  # "Calling", "Messaging", "AI", "Numbers", "Platform"
    default_enabled: bool
    price_metric: str | None  # platform_prices metric that bills it, or None
    description: str


CATALOG: dict[str, Feature] = {
    "voice": Feature(
        "voice", "Calling", "Calling", True, "voice_min_out", "Place and answer phone calls"
    ),
    "sms": Feature("sms", "Text messages", "Messaging", True, "sms_out", "Send and receive SMS"),
    "mms": Feature("mms", "Picture messages", "Messaging", True, "mms_out", "Send and receive MMS"),
    "fax": Feature("fax", "Fax", "Messaging", True, "fax_page_out", "Send and receive faxes"),
    "numbers": Feature(
        "numbers", "Phone numbers", "Numbers", True, "number_mrc", "Order and keep phone numbers"
    ),
    "call_recording": Feature(
        "call_recording",
        "Call recording",
        "Calling",
        False,
        "recording_min",
        "Record customer calls",
    ),
    "call_transcription": Feature(
        "call_transcription",
        "Call transcription",
        "AI",
        False,
        "transcription_min",
        "Transcripts of recorded human calls",
    ),
    "call_summary": Feature(
        "call_summary",
        "AI call summaries",
        "AI",
        False,
        "summary_min",
        "A short AI summary of every transcribed call",
    ),
    "voicemail": Feature(
        "voicemail", "Voicemail", "Calling", True, None, "Voicemail boxes in call flows"
    ),
    "voicemail_transcription": Feature(
        "voicemail_transcription",
        "Voicemail transcription",
        "AI",
        True,
        "transcription_min",
        "Text of every voicemail",
    ),
    "ivr_flows": Feature(
        "ivr_flows",
        "Call flows and menus",
        "Calling",
        True,
        "ivr_month",
        "IVR menus, business hours, routing",
    ),
    "ring_groups": Feature(
        "ring_groups", "Ring groups", "Calling", True, None, "Ring several people at once"
    ),
    "external_transfer": Feature(
        "external_transfer",
        "External transfers",
        "Calling",
        False,
        None,
        "Transfer calls to outside numbers (billed as outbound minutes)",
    ),
    "call_queues": Feature(
        "call_queues", "Call queues", "Calling", True, None, "Queue callers for agents"
    ),
    "supervisor": Feature(
        "supervisor",
        "Supervisor tools",
        "Calling",
        True,
        "supervisor_seat_month",
        "Listen, whisper and barge into calls",
    ),
    "power_dialer": Feature(
        "power_dialer",
        "Power dialer",
        "Calling",
        True,
        "dialer_seat_month",
        "Call lists and campaigns",
    ),
    "amd": Feature(
        "amd",
        "Answering-machine detection",
        "Calling",
        True,
        None,
        "Detect voicemail on outbound calls",
    ),
    "ai_agent": Feature("ai_agent", "AI agent", "AI", True, None, "AI voice and text assistant"),
    "ai_disclosure": Feature(
        "ai_disclosure",
        "AI call disclosure",
        "AI",
        True,
        None,
        "The AI agent says it is an automated assistant at the start of a call",
    ),
    "ai_consent_gate": Feature(
        "ai_consent_gate",
        "AI outbound consent gate",
        "AI",
        False,
        None,
        "AI calling campaigns only dial contacts with a recorded consent for AI calls",
    ),
    "ai_kb": Feature(
        "ai_kb", "AI knowledge base", "AI", True, None, "Documents the AI agent can search"
    ),
    "analytics": Feature(
        "analytics", "Analytics", "Platform", True, None, "Reports and transcript search"
    ),
    "e911": Feature(
        "e911", "Emergency calling (E911)", "Numbers", True, None, "Emergency addresses on numbers"
    ),
    "porting": Feature(
        "porting", "Number porting", "Numbers", True, None, "Move numbers in from another carrier"
    ),
    "tendlc": Feature(
        "tendlc", "10DLC registration", "Messaging", True, None, "Brand and campaign registration"
    ),
    "enterprise_sso": Feature(
        "enterprise_sso",
        "Single sign-on",
        "Platform",
        True,
        "sso_month",
        "SAML / OIDC sign-in and SCIM",
    ),
    "api_access": Feature(
        "api_access", "API and webhooks", "Platform", True, None, "API keys and outbound webhooks"
    ),
    "contact_export": Feature(
        "contact_export", "Contact export", "Platform", True, None, "Export and erase contacts"
    ),
}

#: Features a plan switches off by default (2026-10-06: Starter sees AI agents locked).
PLAN_LOCKED_FEATURES: dict[str, tuple[str, ...]] = {"solo": ("ai_agent",)}


def _memo(session: AsyncSession) -> dict:
    info = session.info
    if "entitlements" not in info:
        info["entitlements"] = {}
    return info["entitlements"]


async def for_org(session: AsyncSession, org_id: uuid.UUID) -> dict[str, bool]:
    """Return every catalog key -> effective value for the org."""
    memo = _memo(session)
    if org_id in memo:
        return memo[org_id]

    stmt = (
        sa.select(OrgFeature.feature_key, OrgFeature.enabled)
        .where(OrgFeature.org_id == org_id)
        .execution_options(**U)
    )
    rows = (await session.execute(stmt)).all()

    result: dict[str, bool] = {key: feature.default_enabled for key, feature in CATALOG.items()}
    plan_code = (
        await session.execute(
            sa.select(Org.plan_code).where(Org.id == org_id).execution_options(**U)
        )
    ).scalar_one_or_none()
    for key in PLAN_LOCKED_FEATURES.get(plan_code or "", ()):
        result[key] = False  # the plan's default; an explicit OrgFeature row below still wins
    for key, enabled in rows:
        result[key] = enabled

    memo[org_id] = result
    return result


async def has(session: AsyncSession, org_id: uuid.UUID, key: str) -> bool:
    if key not in CATALOG:
        raise ValueError(f"unknown feature: {key}")
    values = await for_org(session, org_id)
    return values[key]


async def require(session: AsyncSession, org_id: uuid.UUID, key: str) -> None:
    if key not in CATALOG:
        raise ValueError(f"unknown feature: {key}")
    if not await has(session, org_id, key):
        raise FeatureDisabledError(f"{CATALOG[key].label} is not enabled for your workspace")


async def set_feature(
    session: AsyncSession,
    org_id: uuid.UUID,
    key: str,
    *,
    enabled: bool,
    price_override_micros: int | None,
    actor_user_id: uuid.UUID | None,
) -> OrgFeature:
    """Upsert the explicit feature row for an org. Does NOT commit."""
    if key not in CATALOG:
        raise ValueError(f"unknown feature: {key}")

    set_org_context(session, org_id)

    stmt = sa.select(OrgFeature).where(OrgFeature.org_id == org_id, OrgFeature.feature_key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        row = OrgFeature(
            org_id=org_id,
            feature_key=key,
            enabled=enabled,
            price_override_micros=price_override_micros,
            updated_by=actor_user_id,
        )
        session.add(row)
    else:
        row.enabled = enabled
        row.price_override_micros = price_override_micros
        row.updated_by = actor_user_id

    invalidate(session, org_id)
    return row


def invalidate(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Drop the memo for that org so the next read hits the database."""
    memo = session.info.get("entitlements")
    if memo is not None:
        memo.pop(org_id, None)
