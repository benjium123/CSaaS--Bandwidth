"""Contact resolution and linkage.

``uq_contact_phones_org_e164`` means a phone belongs to at most one contact per org, so
resolution is deterministic and duplicates are prevented by construction rather than
cleaned up later.
"""

from __future__ import annotations

import re
import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import Contact, ContactPhone, CustomFieldDef, MessageThread

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


async def find_contact_by_phone(
    session: AsyncSession, e164: str
) -> tuple[Contact, ContactPhone] | None:
    row = (
        await session.execute(
            sa.select(Contact, ContactPhone)
            .join(ContactPhone, ContactPhone.contact_id == Contact.id)
            .where(ContactPhone.e164 == e164)
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
        elif not isinstance(value, str):
            raise ValidationFailedError(f"Custom field {key!r} expects text")
        clean[key] = value
    return clean


def validate_field_key(key: str) -> str:
    if not KEY_RE.match(key or ""):
        raise ValidationFailedError(
            "Custom field key must be snake_case starting with a letter"
        )
    return key
