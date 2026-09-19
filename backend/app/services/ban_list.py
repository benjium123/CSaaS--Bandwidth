"""P41 platform ban list (fraud_identifiers).

Every value is normalised and SHA-256 hashed before it is stored or compared, so the list
matches across organisations without holding a readable copy of anyone's details. The
``display_hint`` gives an operator just enough to recognise an entry.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models import FRAUD_IDENTIFIER_KINDS, FraudIdentifier

#: Free mailbox providers - banning one of these DOMAINS would ban half the internet, so
#: email_domain identifiers are never derived from them.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "hotmail.com",
        "hotmail.co.uk",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "yandex.ru",
        "mail.ru",
        "qq.com",
        "163.com",
        "tutanota.com",
        "fastmail.com",
        "hey.com",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(kind: str, value: str) -> str:
    v = (value or "").strip().lower()
    if kind in ("email",):
        return v
    if kind in ("email_domain", "website_domain"):
        return domain_of(v) or v
    if kind in ("phone",):
        return re.sub(r"[^0-9+]", "", v)
    if kind in ("registration_number", "tax_id", "address"):
        return _NON_ALNUM.sub("", v)
    return v


def domain_of(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    if "://" not in v:
        v = "http://" + v
    host = urlparse(v).hostname or ""
    return host[4:] if host.startswith("www.") else (host or None)


def address_key(address: dict | None) -> str | None:
    if not address:
        return None
    parts = [
        str(address.get(k) or "") for k in ("line1", "line2", "city", "postal_code", "country")
    ]
    joined = " ".join(p for p in parts if p)
    return joined or None


def hash_value(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{normalize(kind, value)}".encode()).hexdigest()


def hint(kind: str, value: str) -> str:
    v = normalize(kind, value)
    if kind == "email" and "@" in v:
        return "***@" + v.split("@", 1)[1]
    if kind in ("email_domain", "website_domain", "ip"):
        return v[:64]
    if kind == "person":
        return "verified person"
    return ("…" + v[-4:]) if len(v) > 4 else "…"


async def add(
    session: AsyncSession,
    *,
    kind: str,
    value: str | None = None,
    value_hash: str | None = None,
    reason: str,
    source_org_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    display_hint: str | None = None,
) -> FraudIdentifier | None:
    """Add (or reactivate) an identifier. Pass ``value`` or an already-computed
    ``value_hash`` (person identity hashes and device hashes are stored pre-hashed)."""
    if kind not in FRAUD_IDENTIFIER_KINDS:
        raise ValidationFailedError(f"Unknown identifier kind: {kind}")
    if value_hash is None:
        if not value or not normalize(kind, value):
            return None
        value_hash = hash_value(kind, value)
        display_hint = display_hint or hint(kind, value)
    if not reason.strip():
        raise ValidationFailedError("A reason is required")
    existing = (
        await session.execute(
            sa.select(FraudIdentifier).where(
                FraudIdentifier.kind == kind, FraudIdentifier.value_hash == value_hash
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_active = True
        existing.reason = reason[:500]
        return existing
    row = FraudIdentifier(
        id=uuid.uuid4(),
        kind=kind,
        value_hash=value_hash,
        display_hint=(display_hint or "")[:64] or None,
        reason=reason[:500],
        source_org_id=source_org_id,
        created_by=created_by,
        is_active=True,
    )
    session.add(row)
    return row


async def matches(
    session: AsyncSession, identifiers: list[tuple[str, str]]
) -> list[FraudIdentifier]:
    """``identifiers`` is [(kind, value_hash)]. Returns the active ban-list rows hit."""
    pairs = {(k, h) for k, h in identifiers if k and h}
    if not pairs:
        return []
    hashes = {h for _, h in pairs}
    rows = (
        (
            await session.execute(
                sa.select(FraudIdentifier).where(
                    FraudIdentifier.value_hash.in_(hashes), FraudIdentifier.is_active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if (r.kind, r.value_hash) in pairs]


def identifier(kind: str, value: str | None) -> tuple[str, str] | None:
    if not value or not normalize(kind, value):
        return None
    return kind, hash_value(kind, value)
