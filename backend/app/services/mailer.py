"""Minimal transactional email (P41 security alerts and verification notices).

stdlib smtplib run in a thread - no new dependency. With SMTP_HOST unset this is a logged
no-op, never an error: an alert that cannot be emailed is still in the operator queue and
the in-app notification, and a sign-in must never fail because the mail server is down.

``outbox`` collects every message when ``settings.app_env == "test"`` so tests can assert on
what would have been sent without a server.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

#: Test capture. Cleared by tests that assert on it.
outbox: list[EmailMessage] = []


def _build(settings: Settings, to: list[str], subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or f"no-reply@{settings.app_name}"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _send_sync(settings: Settings, msg: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        if smtp.has_extn("starttls"):
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


async def send(settings: Settings, to: list[str], subject: str, body: str) -> bool:
    recipients = sorted({addr.strip() for addr in to if addr and addr.strip()})
    if not recipients:
        return False
    msg = _build(settings, recipients, subject, body)
    if settings.app_env == "test":
        outbox.append(msg)
        return True
    if not settings.smtp_host.strip():
        log.info("email_skipped_no_smtp", subject=subject, recipients=len(recipients))
        return False
    try:
        await asyncio.get_running_loop().run_in_executor(None, _send_sync, settings, msg)
        return True
    except Exception:
        log.warning("email_send_failed", subject=subject, exc_info=True)
        return False
