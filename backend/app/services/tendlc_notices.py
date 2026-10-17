"""10DLC phase 2: tell the workspace owners/admins (email + in-app) when their texting
registration moves, or a number could not be added to the campaign.

One notice per (registration, stage); the dedupe key is ``tendlc:<reg_id>:<stage>`` and
``tendlc:<reg_id>:num:<e164>`` for a number. A notice never breaks the status machine: any
error here is logged and swallowed."""

from __future__ import annotations

import structlog

from app.models import Org
from app.services import billing_alerts

log = structlog.get_logger("tendlc_notices")

NOTIFY_STAGES: dict[str, str] = {
    "brand_filed": "Your texting registration was submitted",
    "brand_approved": "Your business was approved for texting",
    "brand_rejected": "Your texting registration needs changes",
    "campaign_filed": "Your texting campaign was submitted for carrier review",
    "active": "Your texting campaign is approved - you can text now",
    "campaign_rejected": "Your texting campaign needs changes",
    "needs_attention": "Your texting registration needs attention",
}
NUMBER_FAILED_SUBJECT = "A number could not be added to your texting campaign"
_REJECTED = {"brand_rejected": "brand", "campaign_rejected": "campaign"}
_LINK_PATH = "/settings/messaging"


def _link(base: str) -> str:
    return f"Review it here: {(base or '').rstrip('/')}{_LINK_PATH}"


def body_for(reg, stage: str, base: str = "") -> str:  # noqa: ANN001
    """Email body: the subject sentence, the registration detail (carrier reason plus the
    refund/stop text) when there is one, then the link line."""
    parts = [NOTIFY_STAGES[stage]]
    detail = (getattr(reg, "detail", None) or "").strip()
    if detail:
        parts.append(detail)
    parts.append(_link(base))
    return "\n\n".join(parts)


def _first_reason(reg, stage: str) -> str:  # noqa: ANN001
    rejection = reg.rejection or {}
    if rejection.get("subject") != _REJECTED.get(stage):
        return ""
    reasons = rejection.get("reasons")
    for r in reasons if isinstance(reasons, list) else []:
        if isinstance(r, str) and r.strip():
            return r.strip()
    return ""


async def _send(session, settings, reg, subject: str, in_app: str, body: str,  # noqa: ANN001
                dedupe_key: str) -> bool:
    org = await session.get(Org, reg.org_id)
    if org is None:
        return False
    await billing_alerts.notify_owners(
        session, settings, org, subject, body, dedupe_key=dedupe_key, kind="texting",
        in_app_body=in_app[:255], email_only_if_new=True,
    )
    await session.commit()
    return True


async def _swallow(session, reg) -> None:  # noqa: ANN001
    log.exception("tendlc_notice_failed", registration_id=str(getattr(reg, "id", "")))
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        pass


async def stage_changed(session, settings, reg, old_stage: str, new_stage: str) -> bool:  # noqa: ANN001
    try:
        if new_stage not in NOTIFY_STAGES or old_stage == new_stage:
            return False
        if new_stage == "brand_approved" and (reg.filing or {}).get("brand_reused"):
            return False  # campaign-only resubmit: the business was already approved
        subject = NOTIFY_STAGES[new_stage]
        reason = _first_reason(reg, new_stage)
        in_app = f"{subject}: {reason}" if reason else subject
        base = getattr(settings, "public_web_url", "") or ""
        return await _send(
            session, settings, reg, subject, in_app, body_for(reg, new_stage, base),
            f"tendlc:{reg.id}:{new_stage}",
        )
    except Exception:
        await _swallow(session, reg)
        return False


async def number_failed(session, settings, reg, e164: str, error: str) -> bool:  # noqa: ANN001
    try:
        base = getattr(settings, "public_web_url", "") or ""
        body = "\n\n".join(
            [
                NUMBER_FAILED_SUBJECT,
                f"The number {e164} could not be added: {(error or '').strip()[:300]}",
                _link(base),
            ]
        )
        return await _send(
            session, settings, reg, NUMBER_FAILED_SUBJECT, f"{NUMBER_FAILED_SUBJECT}: {e164}",
            body, f"tendlc:{reg.id}:num:{e164}",
        )
    except Exception:
        await _swallow(session, reg)
        return False
