"""Contacts, companies, tags, notes, custom-field definitions.

Data-model shape harvested from Chatwoot's contact/conversation/label model, translated to
our stack. Every table rides ``TenantScoped``.

The load-bearing constraint here is ``uq_contact_phones_org_e164``: one phone number belongs
to at most one contact per org. That makes phone→contact resolution deterministic and is the
structural guard against the most common duplicate vector — so P11's merge becomes a
re-parenting exercise rather than data repair.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

CUSTOM_FIELD_KINDS = ("text", "number", "date", "select")


class Company(Base, TenantScoped, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    attributes: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class Contact(Base, TenantScoped, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (sa.Index("ix_contacts_org_display_name", "org_id", "display_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    first_name: Mapped[str | None] = mapped_column(sa.String(127), nullable=True)
    last_name: Mapped[str | None] = mapped_column(sa.String(127), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    # Custom-field VALUES. Definitions live in custom_field_defs. JSON, not EAV: v1 has no
    # per-field indexed query requirement, and EAV would cost joins + SQLite parity for
    # nothing we ship this year.
    attributes: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class ContactPhone(Base, TenantScoped, TimestampMixin):
    __tablename__ = "contact_phones"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "e164", name="uq_contact_phones_org_e164"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    e164: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(31), nullable=False, default="mobile")
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class Tag(Base, TenantScoped, TimestampMixin):
    """One tags table for both contacts and threads. A tag is a tag."""

    __tablename__ = "tags"
    __table_args__ = (sa.UniqueConstraint("org_id", "name", name="uq_tags_org_name"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    color: Mapped[str] = mapped_column(sa.String(7), nullable=False, default="#64748b")


class ContactTag(Base, TenantScoped, TimestampMixin):
    __tablename__ = "contact_tags"
    __table_args__ = (
        sa.UniqueConstraint("contact_id", "tag_id", name="uq_contact_tags"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )


class ThreadLabel(Base, TenantScoped, TimestampMixin):
    __tablename__ = "thread_labels"
    __table_args__ = (sa.UniqueConstraint("thread_id", "tag_id", name="uq_thread_labels"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )


class ContactNote(Base, TenantScoped, TimestampMixin):
    __tablename__ = "contact_notes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)


class CustomFieldDef(Base, TenantScoped, TimestampMixin):
    __tablename__ = "custom_field_defs"
    __table_args__ = (sa.UniqueConstraint("org_id", "key", name="uq_custom_field_defs_org_key"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(15), nullable=False, default="text")
    options: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
