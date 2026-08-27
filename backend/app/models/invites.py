"""Invitations: the only way to get an account once the instance has an owner.

Two properties do the security work here, and both are deliberate:

* **Only a HASH of the token is stored.** An invite token is a bearer credential - whoever
  holds it can create an account. Storing it in plaintext would mean a database read (a
  backup, a log, a support query) hands out working invitations. Same reasoning as a
  password; same treatment.
* **The invite is bound to an EMAIL.** A token that leaks cannot be redeemed by whoever
  found it, only by the address it was issued to. Without that binding an intercepted
  link is a free account on someone else's instance.

Single-use (`accepted_at`) and time-limited (`expires_at`) are the other two halves of a
credential that should be as short-lived as the job it was issued for.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class Invite(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Lowercased at write time. Redemption compares lowercased, so casing in the mail
    #: client can never be the reason an invitation "does not work".
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, index=True)
    #: Which system role the invitee lands in. Validated against SYSTEM_ROLES at creation -
    #: an invite must never be able to mint a role that does not exist, or "owner" by typo.
    role_name: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="agent")
    #: sha256 of the raw token. The raw value is returned ONCE, at creation, and is never
    #: recoverable from this row.
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    #: Set on redemption. Non-null means spent; an invite is never reusable.
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Who issued it, for the audit question "who let this person in?"
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Set when an admin cancels an unredeemed invite.
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.Index("ix_invites_org_email", "org_id", "email"),)
