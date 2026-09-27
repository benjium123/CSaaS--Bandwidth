"""Telnyx email delivery follow-up.

Telnyx answers 202 and can then fail the message. Production behaviour for 2FA-code
emails: 2 of the last 14 failed. A failed message has events queued, sending,
injection_timeout, failed and recipient_statuses containing injection_timeout. A
delivered one has events queued, sending, sent, delivered and recipient_statuses
containing delivered. Failures show up within ~1 s; delivery confirms in ~35 s. While
in flight, recipient_statuses may be empty or hold queued/sending/sent.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import structlog

from app.config import Settings

log = structlog.get_logger("email_delivery")

CHECK_DELAYS_SECONDS = (15, 45, 120)
ALERT_COOLDOWN_SECONDS = 1800
#: "deferred" = the receiving server asked to try later; the mail is still in transit and
#: normally arrives, so it is never re-sent (a re-send is a guaranteed duplicate).
PENDING_STATUSES = frozenset({"queued", "sending", "sent", "deferred"})
FAILED_EVENT_TYPES = frozenset(
    {"failed", "bounced", "dropped", "rejected", "injection_timeout", "undeliverable"}
)

_tasks: set[asyncio.Task] = set()
_last_alert_at: float | None = None


def classify(message: dict) -> tuple[str, list[str]]:
    if not isinstance(message, dict):
        return "pending", []

    events = message.get("events")
    recipient_statuses = message.get("recipient_statuses")
    reasons: set[str] = set()

    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                event_type = event.get("type")
                if event_type in FAILED_EVENT_TYPES:
                    reasons.add(event_type)

    if isinstance(recipient_statuses, dict):
        for status in recipient_statuses:
            if status not in PENDING_STATUSES and status != "delivered":
                reasons.add(status)

    if reasons:
        return "failed", sorted(reasons)

    if (
        isinstance(recipient_statuses, dict)
        and recipient_statuses
        and all(status == "delivered" for status in recipient_statuses)
    ):
        return "delivered", []

    return "pending", []


async def fetch_message(settings: Settings, message_id: str) -> dict | None:
    key = settings.telnyx_api_key.get_secret_value()
    if not key:
        return None
    url = f"https://api.telnyx.com/v2/email_messages/{message_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            data = payload.get("data")
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def mask(text: str) -> str:
    return re.sub(r"\d{4,}", "######", text)


def track(
    settings: Settings,
    message_id: str,
    recipients: list[str],
    subject: str,
    body: str,
    *,
    attempt: int = 1,
) -> None:
    if settings.app_env == "test" or not message_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("email_delivery_track_no_loop")
        return
    task = loop.create_task(_follow_up(settings, message_id, recipients, subject, body, attempt))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _follow_up(
    settings: Settings,
    message_id: str,
    recipients: list[str],
    subject: str,
    body: str,
    attempt: int,
) -> None:
    try:
        seen = False
        for delay in CHECK_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            message = await fetch_message(settings, message_id)
            if message is None:
                continue
            seen = True

            verdict, reasons = classify(message)
            if verdict == "delivered":
                log.info("email_delivered", message_id=message_id, attempt=attempt)
                return
            if verdict == "failed":
                break
        else:
            # Never confirmed delivered (still queued/sent/deferred): it is most likely still
            # on its way, so it is NOT re-sent - that would reach the person twice. Operators
            # get one (rate-limited) notice instead of the mail being dropped silently.
            log.warning(
                "email_delivery_unconfirmed",
                message_id=message_id,
                attempt=attempt,
            )
            if not seen:
                return  # the status API never answered: no evidence either way
            await _alert_operators(settings, recipients, subject, ["unconfirmed"])
            return

        log.warning(
            "email_delivery_failed",
            message_id=message_id,
            reasons=reasons,
            attempt=attempt,
            recipients=len(recipients),
            subject=mask(subject),
        )
        if attempt == 1:
            await _retry(settings, recipients, subject, body)
        else:
            await _alert_operators(settings, recipients, subject, reasons)
    except Exception:
        log.warning("email_follow_up_crashed", exc_info=True)


async def _retry(
    settings: Settings,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    from app.services import mailer

    if settings.resend_api_key.get_secret_value():
        ok = await mailer._resend_post(settings, recipients, subject, body)
        log.info("email_retried", via="resend", ok=ok)
        if not ok:
            await _alert_operators(settings, recipients, subject, ["retry_failed"])
        return

    accepted, new_id = await mailer._telnyx_post(settings, recipients, subject, body)
    log.info("email_retried", via="telnyx", ok=accepted)
    if accepted and new_id:
        track(settings, new_id, recipients, subject, body, attempt=2)
    elif not accepted:
        await _alert_operators(settings, recipients, subject, ["retry_failed"])


async def _operator_emails() -> list[str]:
    try:
        import sqlalchemy as sa

        from app.db.session import get_sessionmaker
        from app.models import PlatformOperator, User

        stmt = (
            sa.select(User.email)
            .select_from(PlatformOperator)
            .join(User, PlatformOperator.user_id == User.id)
            .where(
                PlatformOperator.is_active.is_(True),
                PlatformOperator.role == "admin",
            )
            .execution_options(allow_unscoped=True)
        )

        async with get_sessionmaker()() as session:
            result = await session.execute(stmt)
            emails = result.scalars().all()

        return sorted({email for email in emails if email})
    except Exception:
        log.warning("email_operator_lookup_failed", exc_info=True)
        return []


async def _alert_operators(
    settings: Settings,
    recipients: list[str],
    subject: str,
    reasons: list[str],
) -> None:
    global _last_alert_at

    now = time.monotonic()
    if _last_alert_at is not None and now - _last_alert_at < ALERT_COOLDOWN_SECONDS:
        log.info("email_alert_suppressed")
        return
    _last_alert_at = now

    emails = await _operator_emails()
    if not emails:
        log.warning("email_alert_no_operators")
        return

    domains = sorted({addr.rsplit("@", 1)[1].lower() for addr in recipients if "@" in addr})
    domain_list = ", ".join(domains)
    reason_list = ", ".join(reasons)

    body = (
        "A Ringlite email could not be delivered, even after a retry.\n\n"
        f"Subject: {mask(subject)}\n"
        f"Recipient domain(s): {domain_list}\n"
        f"Reason: {reason_list}\n\n"
        "The customer may not have received their code or notice. Check the Telnyx email log and "
        "the SPF/DMARC records for mail.ringlite.io. Further failures in the next 30 minutes will "
        "not send another alert."
    )

    from app.services import mailer

    await mailer.send(
        settings,
        emails,
        "Ringlite: an email failed to deliver",
        body,
        follow_up=False,
    )
    log.info("email_alert_sent", count=len(emails))
