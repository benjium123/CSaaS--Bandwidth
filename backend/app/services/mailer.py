"""Minimal transactional email (P41 security alerts and verification notices).

stdlib smtplib run in a thread - no new dependency. With SMTP_HOST unset this is a logged
no-op, never an error: an alert that cannot be emailed is still in the operator queue and
the in-app notification, and a sign-in must never fail because the mail server is down.

``outbox`` collects every message when ``settings.app_env == "test"`` so tests can assert on
what would have been sent without a server.
"""

from __future__ import annotations

import asyncio
import html
import re
import smtplib
import ssl
from email.message import EmailMessage

import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

#: Test capture. Cleared by tests that assert on it.
outbox: list[EmailMessage] = []


def _html(body: str, subject: str = "", app_name: str = "Ringlite") -> str:
    """Branded HTML (button for the action link, code box for one-time codes); falls back
    to the plain paragraph version if the template ever fails - an email must still go out."""
    try:
        from app.services import email_template

        return email_template.render(subject, body, app_name=app_name)
    except Exception:
        log.warning("email_template_failed", exc_info=True)
        return _plain_html(body)


def _plain_html(body: str) -> str:
    def paragraph(value: str) -> str:
        escaped = html.escape(value).replace(chr(10), "<br>")
        return re.sub(
            r"https://[^\s<]+",
            lambda m: f'<a href="{m.group(0)}" style="color:#2563eb">{m.group(0)}</a>',
            escaped,
        )

    paragraphs = "".join(
        f'<p style="margin:0 0 12px">{paragraph(p)}</p>' for p in body.split("\n\n")
    )
    return (
        '<!doctype html><html><body style="font-family:system-ui,-apple-system,Segoe UI,'
        'sans-serif;font-size:14px;line-height:1.5;color:#111">'
        f"{paragraphs}</body></html>"
    )


def _build(settings: Settings, to: list[str], subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or f"no-reply@{settings.app_name}"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(_html(body, subject, settings.app_name), subtype="html")
    return msg


def _send_sync(settings: Settings, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=15, context=context
        ) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(msg)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        if smtp.has_extn("starttls"):
            smtp.starttls(context=context)
            smtp.ehlo()
        elif settings.is_production:
            # P42: security emails carry reset links - never send them in the clear.
            raise RuntimeError("SMTP server does not offer STARTTLS; refusing to send")
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


async def _telnyx_post(
    settings: Settings, recipients: list[str], subject: str, body: str
) -> tuple[bool, str | None]:
    """POST to Telnyx. Returns (accepted, message id). A 202 is NOT delivery - Telnyx can
    still fail the message moments later (email_delivery follows it up)."""
    import httpx

    key = settings.telnyx_api_key.get_secret_value()
    if not key:
        return False, None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.post(
                "https://api.telnyx.com/v2/email_messages",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "from": settings.telnyx_email_from,
                    "from_name": "Ringlite",
                    "to": recipients,
                    "subject": subject,
                    "text_body": body,
                    "html_body": _html(body, subject, settings.app_name),
                    "tracking_settings": {"open_tracking": False, "click_tracking": False},
                },
            )
            result.raise_for_status()
    except httpx.HTTPError:
        log.warning("email_telnyx_failed", recipients=len(recipients))
        return False, None
    try:
        message_id = (result.json().get("data") or {}).get("id")
    except (ValueError, AttributeError):
        message_id = None
    return True, message_id if isinstance(message_id, str) else None


async def _resend_post(settings: Settings, recipients: list[str], subject: str, body: str) -> bool:
    import httpx

    sender = settings.resend_from or settings.smtp_from
    if not sender:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}"},
                json={
                    "from": sender,
                    "to": recipients,
                    "subject": subject,
                    "text": body,
                    "html": _html(body, subject, settings.app_name),
                },
            )
            result.raise_for_status()
        return True
    except httpx.HTTPError:
        log.warning("email_resend_failed", recipients=len(recipients))
        return False


async def send(
    settings: Settings, to: list[str], subject: str, body: str, *, follow_up: bool = True
) -> bool:
    """``follow_up=False`` skips the Telnyx delivery check (used by the delivery-failure
    alert itself, so a failing alert can never alert again)."""
    recipients = sorted({addr.strip() for addr in to if addr and addr.strip()})
    if not recipients:
        return False
    msg = _build(settings, recipients, subject, body)
    if settings.app_env == "test":
        outbox.append(msg)
        return True
    resend_on = bool(settings.resend_api_key.get_secret_value())
    # Resend first when configured: it reaches Gmail in seconds, Telnyx's MTA took 18-66 s
    # (measured 2026-09-27). Telnyx stays the fallback when Resend refuses the message.
    if resend_on and await _resend_post(settings, recipients, subject, body):
        return True
    if settings.telnyx_email_from:
        accepted, message_id = await _telnyx_post(settings, recipients, subject, body)
        if accepted and follow_up and message_id:
            from app.services import email_delivery

            email_delivery.track(settings, message_id, recipients, subject, body)
        return accepted
    if resend_on:
        return False
    if not settings.smtp_host.strip():
        log.info("email_skipped_no_smtp", subject=subject, recipients=len(recipients))
        return False
    try:
        await asyncio.get_running_loop().run_in_executor(None, _send_sync, settings, msg)
        return True
    except Exception:
        log.warning("email_send_failed", subject=subject, exc_info=True)
        return False
