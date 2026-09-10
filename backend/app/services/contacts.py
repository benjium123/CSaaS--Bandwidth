"""Contact resolution and linkage.

``uq_contact_phones_org_e164`` means a phone belongs to at most one contact per org, so
resolution is deterministic and duplicates are prevented by construction rather than
cleaned up later.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import Contact, ContactPhone, CustomFieldDef, MessageThread

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def active_contacts_filter() -> sa.ColumnElement:
    """A merged loser row is kept for history but is hidden from every contact list,
    search, phone lookup and conversation card. This is the ONE place that rule is
    written; every query that reads contacts must apply it."""
    return Contact.merged_into_contact_id.is_(None)


async def find_contact_by_phone(
    session: AsyncSession, e164: str
) -> tuple[Contact, ContactPhone] | None:
    row = (
        await session.execute(
            sa.select(Contact, ContactPhone)
            .join(ContactPhone, ContactPhone.contact_id == Contact.id)
            .where(ContactPhone.e164 == e164)
            .where(active_contacts_filter())
        )
    ).first()
    return (row[0], row[1]) if row else None


async def resolve_or_create_contact(
    session: AsyncSession, org_id: uuid.UUID, e164: str
) -> Contact:
    """Get the contact owning ``e164``, creating a minimal one if none does.

    Race-safe the same way ``upsert_thread`` is: insert, catch the unique violation that a
    concurrent inbound would cause, re-select.
    """
    found = await find_contact_by_phone(session, e164)
    if found is not None:
        return found[0]

    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164, attributes={})
    # P22: lets the inbound path tell "just created here" from "already existed" without
    # changing this function's signature (list_import calls it too). Transient attribute.
    contact._just_created = True  # type: ignore[attr-defined]
    session.add(contact)
    try:
        await session.flush()
        session.add(
            ContactPhone(
                id=uuid.uuid4(),
                org_id=org_id,
                contact_id=contact.id,
                e164=e164,
                label="mobile",
                is_primary=True,
            )
        )
        await session.flush()
    except IntegrityError:
        await session.rollback()
        set_org_context(session, org_id)
        found = await find_contact_by_phone(session, e164)
        if found is None:  # pragma: no cover - only if the row vanished between attempts
            raise
        return found[0]
    return contact


async def link_threads_for_phone(
    session: AsyncSession, org_id: uuid.UUID, e164: str, contact_id: uuid.UUID
) -> int:
    """Stamp existing threads for this number with the contact.

    Used when a contact is created or edited AFTER messages already exist — the common
    real-world order. Idempotent.
    """
    result = await session.execute(
        sa.update(MessageThread)
        .where(
            MessageThread.contact_e164 == e164,
            sa.or_(
                MessageThread.contact_id.is_(None),
                MessageThread.contact_id != contact_id,
            ),
        )
        .values(contact_id=contact_id)
    )
    return result.rowcount or 0


#: Contact-detail keys the P16 contact panel stores in ``attributes`` without an org
#: custom-field definition. Text only.
BUILTIN_CONTACT_ATTRIBUTES: frozenset[str] = frozenset({"company", "role", "email", "address"})


async def validate_attributes(
    session: AsyncSession, attributes: dict | None
) -> dict:
    """Validate custom-field values against the org's definitions."""
    if not attributes:
        return {}

    defs = {
        d.key: d
        for d in (await session.execute(sa.select(CustomFieldDef))).scalars().all()
    }
    clean: dict = {}
    for key, value in attributes.items():
        # 5(c): built-ins are checked BEFORE any custom-field definition. A colliding
        # definition may pre-date the 5.16 create-time guard (or have been inserted
        # directly) - it must never shadow the built-in P16 kind/validation.
        if key in BUILTIN_CONTACT_ATTRIBUTES:
            if value is not None and not isinstance(value, str):
                raise ValidationFailedError(f"Contact field {key!r} expects text")
            clean[key] = value
            continue
        definition = defs.get(key)
        if definition is None:
            raise ValidationFailedError(f"Unknown custom field: {key!r}")
        if value is None:
            clean[key] = None
            continue
        if definition.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationFailedError(f"Custom field {key!r} expects a number")
        elif definition.kind == "select":
            if value not in (definition.options or []):
                raise ValidationFailedError(
                    f"Custom field {key!r} must be one of: {', '.join(definition.options or [])}"
                )
        elif definition.kind == "date":
            # 5.15: a "date" custom field previously accepted ANY string - nothing ever
            # parsed it, so an unparseable value only broke whatever read it back later.
            if not isinstance(value, str):
                raise ValidationFailedError(f"Custom field {key!r} expects a date string")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationFailedError(
                    f"Custom field {key!r} must be a valid date (YYYY-MM-DD)"
                ) from exc
        elif not isinstance(value, str):
            raise ValidationFailedError(f"Custom field {key!r} expects text")
        clean[key] = value
    return clean


def validate_field_key(key: str) -> str:
    if not KEY_RE.match(key or ""):
        raise ValidationFailedError(
            "Custom field key must be snake_case starting with a letter"
        )
    if key in BUILTIN_CONTACT_ATTRIBUTES:
        # 5.16: a custom field sharing a key with a P16 built-in attribute (company,
        # role, email, address) would collide with it in validate_attributes above -
        # this definition's own kind/options would never actually be enforced, since the
        # built-in branch is checked first and wins.
        raise ValidationFailedError(
            f"{key!r} is a built-in contact field and cannot be redefined as a custom field"
        )
    return key
