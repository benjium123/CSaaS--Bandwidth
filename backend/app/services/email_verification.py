"""Single-use, expiring confirmation links; only token hashes are stored."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.services import mailer


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def send_confirmation(session, settings, user) -> bool:
    import sqlalchemy as sa

    from app.models import User

    user = (
        await session.execute(
            sa.select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    now = datetime.now(timezone.utc)
    sent = user.email_verification_sent_at
    if sent and now - sent.replace(tzinfo=timezone.utc) < timedelta(seconds=60):
        return True
    token = secrets.token_urlsafe(32)
    link = f"{settings.public_web_url.rstrip('/')}/confirm-email?token={token}"
    delivered = await mailer.send(
        settings,
        [user.email],
        "Confirm your Ringlite email",
        f"Welcome to Ringlite. Confirm your email address to continue.\n\n{link}\n\n"
        "This link expires in 24 hours. If you did not sign up, ignore this email.",
    )
    if delivered:
        user.email_verification_hash = digest(token)
        user.email_verification_expires_at = now + timedelta(hours=24)
        user.email_verification_sent_at = now
    await session.commit()
    return delivered
