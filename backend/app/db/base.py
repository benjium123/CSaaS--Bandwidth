"""Declarative base, the tenant mixin, and the session-level isolation guard.

This is the heart of P0. Tenant isolation is NOT a convention that every query author must
remember — it is enforced mechanically by two SQLAlchemy event listeners:

  1. ``do_orm_execute``  — injects ``org_id = <ctx>`` into every ORM SELECT/UPDATE/DELETE
     that touches a TenantScoped model, and RAISES if no org context is set.
  2. ``before_flush``    — rejects writing a TenantScoped row whose ``org_id`` differs from
     the session's context. ``with_loader_criteria`` covers reads only; this covers writes.

Escape hatch: ``session.info["allow_unscoped"] = True`` or
``.execution_options(allow_unscoped=True)``. Every use must carry a comment justifying it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, with_loader_criteria

from app.db.types import GUID
from app.errors import MissingTenantContextError

ORG_CONTEXT_KEY = "org_id"
ALLOW_UNSCOPED_KEY = "allow_unscoped"


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class TenantScoped:
    """Mixin marking a model as belonging to exactly one org.

    Declaring this mixin is the whole contract: the listeners below then guarantee the row
    can never be read or written outside its org.
    """

    @sa.orm.declared_attr
    def org_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
        )


def set_org_context(session: Any, org_id: uuid.UUID | None) -> None:
    """Bind an org to a session. Accepts AsyncSession or Session."""
    info = _info_of(session)
    info[ORG_CONTEXT_KEY] = org_id


def allow_unscoped(session: Any, value: bool = True) -> None:
    """Deliberately disable the tenant guard on this session. Justify every call site."""
    _info_of(session)[ALLOW_UNSCOPED_KEY] = value


def _info_of(session: Any) -> dict:
    sync = getattr(session, "sync_session", None)
    return (sync or session).info


def _touches_tenant_model(execute_state: Any) -> bool:
    for mapper in execute_state.all_mappers:
        if issubclass(mapper.class_, TenantScoped):
            return True
    return False


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(execute_state: Any) -> None:
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return
    if not _touches_tenant_model(execute_state):
        return

    info = execute_state.session.info
    if execute_state.execution_options.get(ALLOW_UNSCOPED_KEY) or info.get(ALLOW_UNSCOPED_KEY):
        return

    org_id = info.get(ORG_CONTEXT_KEY)
    if org_id is None:
        raise MissingTenantContextError(
            "A tenant-scoped query ran without an org context. Bind one with "
            "set_org_context(session, org_id), or pass allow_unscoped=True and justify it."
        )

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.org_id == org_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _guard_cross_tenant_writes(session: Session, flush_context: Any, instances: Any) -> None:
    if session.info.get(ALLOW_UNSCOPED_KEY):
        return
    ctx = session.info.get(ORG_CONTEXT_KEY)

    pending = [o for o in session.new if isinstance(o, TenantScoped)]
    changed = [o for o in session.dirty if isinstance(o, TenantScoped)]

    for obj in pending + changed:
        if ctx is None:
            raise MissingTenantContextError(
                f"Refusing to write {type(obj).__name__} with no org context on the session."
            )
        obj_org = getattr(obj, "org_id", None)
        if obj_org is None:
            # Stamp it rather than fail: the session's org is unambiguous.
            obj.org_id = ctx
        elif obj_org != ctx:
            raise MissingTenantContextError(
                f"Refusing cross-tenant write: {type(obj).__name__}.org_id={obj_org} "
                f"but the session is scoped to org {ctx}."
            )
