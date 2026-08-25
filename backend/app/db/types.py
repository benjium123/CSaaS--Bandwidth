"""Portable column types.

GUARD 1 (phase-0-plan DR-1). Local tests run on SQLite, production runs on Postgres. These
types are the ONLY place ``sqlalchemy.dialects.postgresql`` may be imported in app code —
Ruff's banned-api rule enforces that everywhere else, so Postgres-only SQL cannot hide in a
code path the SQLite suite would not exercise.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: TID251
from sqlalchemy.types import CHAR, TypeDecorator


class GUID(TypeDecorator):
    """UUID that is native on Postgres and CHAR(36) elsewhere.

    Values are always ``uuid.UUID`` in Python, always a canonical lowercase hyphenated
    string on the wire for SQLite. PKs are generated in the app (uuid4) so no database
    extension is required on either backend.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001, ANN201
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


def PortableJSON() -> sa.types.TypeEngine:  # noqa: N802 - reads as a type constructor
    """JSON that becomes JSONB on Postgres and JSON elsewhere."""
    return sa.JSON().with_variant(JSONB, "postgresql")  # noqa: TID251
