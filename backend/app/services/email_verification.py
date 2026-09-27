"""Email-address confirmation: a six-digit emailed code (see services/email_code.py).

`digest` is kept for the legacy link columns, which are no longer written."""

import hashlib
from datetime import datetime, timezone


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def send_confirmation(session, settings, user) -> bool:
    """Email a six-digit code (email_code purpose "verify") that confirms the address.

    Replaces the old 24 h link. email_code.issue throttles re-sends (30 s) and says so.
    """
    import sqlalchemy as sa

    from app.models import User
    from app.services import email_code

    user = (
        await session.execute(
            sa.select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    delivered = await email_code.issue(settings, user, "verify")
    if delivered:
        user.email_verification_sent_at = datetime.now(timezone.utc)
    await session.commit()
    return delivered
