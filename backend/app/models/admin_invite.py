"""Platform-scoped admin invitations (P41 follow-up).

An AdminInvite is a one-time, platform-scoped invitation to become a platform operator.
It is deliberately NOT a WebAuthn challenge and NOT a tenant-scoped Invite: it belongs to
no org, it is issued only from a trusted server context (scripts/invite_admin.py), and it
carries only the SHA-256 of the token - never the plaintext.

Like models/security.py, this table is not TenantScoped. Every query filters explicitly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID

#: How an invitation was issued. Only the trusted CLI exists today; the column is kept so
#: a future issuance path can be told apart in the audit trail without a migration.
ADMIN_INVITE_ISSUED_VIA: tuple[str, ...] = ("trusted_cli",)


class AdminInvite(Base, TimestampMixin):
    """A single-use invitation to become a platform operator.

    ``token_hash`` is the SHA-256 hex digest of the token handed to the operator; the
    plaintext token exists only in the URL the CLI prints and is never stored.
    """

    __tablename__ = "admin_invites"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Normalised (lowercased, stripped) email the invitation is bound to.
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, index=True)
    #: SHA-256 hex digest of the token. Unique so a token can be looked up directly.
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    #: The user who accepted the invitation, if known at consume time. SET NULL so deleting
    #: the user does not erase the fact that the invitation was used.
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    issued_via: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="trusted_cli", server_default="trusted_cli"
    )
