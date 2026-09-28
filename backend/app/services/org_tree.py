"""Org hierarchy (0092): who reports to whom inside ONE workspace.

``OrgMembership.reports_to_user_id`` names each member's manager. Every query here runs
under the org context already bound to the session (OrgMembership is TenantScoped), so
a manager id from another workspace can never be followed. The whole tree is loaded in
one query and walked in memory; the ``seen`` set makes a cycle (which the API refuses to
write anyway) terminate instead of looping.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrgMembership


async def _edges(session: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    return list(
        (
            await session.execute(
                sa.select(OrgMembership.user_id, OrgMembership.reports_to_user_id)
            )
        ).all()
    )


async def descendants(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Everyone below ``user_id``, at any depth. Never includes ``user_id`` itself."""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for member, manager in await _edges(session):
        if manager is not None:
            children.setdefault(manager, []).append(member)
    seen: set[uuid.UUID] = set()
    stack = list(children.get(user_id, []))
    while stack:
        u = stack.pop()
        if u in seen or u == user_id:
            continue
        seen.add(u)
        stack.extend(children.get(u, []))
    return seen


async def ancestors(session: AsyncSession, user_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Every manager above any of ``user_ids``, at any depth (excluding the inputs)."""
    edges = await _edges(session)
    manager_of = {m: mgr for m, mgr in edges if mgr is not None}
    members = {m for m, _ in edges}
    out: set[uuid.UUID] = set()
    for start in user_ids:
        u = manager_of.get(start)
        while u is not None and u not in out and u not in user_ids:
            # A manager who left the workspace carries no access (and has no row here).
            if u not in members:
                break
            out.add(u)
            u = manager_of.get(u)
    return out


async def detach_reports(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Nobody reports to someone who has left: re-adding them later must not silently hand
    back every line of their old reports. Call on EVERY path that removes a membership."""
    await session.execute(
        sa.update(OrgMembership)
        .where(OrgMembership.org_id == org_id, OrgMembership.reports_to_user_id == user_id)
        .values(reports_to_user_id=None)
    )


async def would_cycle(
    session: AsyncSession, user_id: uuid.UUID, manager_id: uuid.UUID
) -> bool:
    """True if making ``manager_id`` the manager of ``user_id`` closes a loop."""
    return manager_id == user_id or manager_id in await descendants(session, user_id)
