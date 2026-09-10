"""Capabilities endpoint for the current caller and org.

This powers nav gating and the first-run onboarding checklist. The backend remains the
authority for every permission check - the response here is a UI hint, never an
enforcement point.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.deps import OrgContext, get_current_org
from app.compliance import registration
from app.errors import ValidationFailedError
from app.models import PERMISSIONS, OrgMembership, OrgNumber, ProviderAccount
from app.services import notifications as notifications_svc

router = APIRouter(prefix="/api/v1/me", tags=["me"])


class OrgSummaryOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    has_provider: bool
    has_number: bool
    member_count: int
    registration_state: str


class CapabilitiesOut(BaseModel):
    permissions: list[str]
    org: OrgSummaryOut


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    thread_id: uuid.UUID | None
    our_e164: str | None
    contact_e164: str | None
    body: str
    read_at: datetime | None
    created_at: datetime


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int


class NotificationsReadIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ids: list[uuid.UUID] | None = None
    # `all` on the wire; `all_` in Python so it never shadows the builtin.
    all_: bool = Field(default=False, alias="all")


@router.get("/capabilities", response_model=CapabilitiesOut)
async def capabilities(
    ctx: Annotated[OrgContext, Depends(get_current_org)],
) -> CapabilitiesOut:
    # Deliberately NOT require_permission("org:read/settings:read"): the agent role holds
    # neither, and this endpoint exists to tell a caller what it itself may do.
    permissions: list[str]
    role_permissions = ctx.role.permissions or []
    if "*" in role_permissions:
        permissions = sorted(PERMISSIONS)
    else:
        permissions = sorted(set(role_permissions))

    provider_row = (
        await ctx.session.execute(
            sa.select(ProviderAccount.id)
            .where(ProviderAccount.status.in_(("active", "unverified")))
            .limit(1)
        )
    ).scalar_one_or_none()
    has_provider = provider_row is not None

    number_row = (
        await ctx.session.execute(
            sa.select(OrgNumber.id).where(OrgNumber.status == "active").limit(1)
        )
    ).scalar_one_or_none()
    has_number = number_row is not None

    # The tenant guard attaches its criteria to the ENTITIES in a statement, so a bare
    # `func.count()` over `select_from(OrgMembership)` would count every org's members.
    # Counting a MAPPED COLUMN keeps the guard's org filter attached.
    member_count = (
        await ctx.session.execute(sa.select(sa.func.count(OrgMembership.id)))
    ).scalar_one()

    active_numbers = (
        await ctx.session.execute(
            sa.select(OrgNumber).where(OrgNumber.status == "active").limit(50)
        )
    ).scalars().all()

    # Scan at most the first 50 active numbers; the precedence is pending > rejected >
    # approved, with any unknown falling through to "unknown".
    registration_state = "none"
    if active_numbers:
        any_pending = False
        any_rejected = False
        all_approved = True
        for number in active_numbers:
            state = await registration.registration_state(ctx.session, number)
            if state.verdict == "pending":
                any_pending = True
                break
            if state.verdict == "rejected":
                any_rejected = True
            if state.verdict != "approved":
                all_approved = False

        if any_pending:
            registration_state = "pending"
        elif any_rejected:
            registration_state = "rejected"
        elif all_approved:
            registration_state = "approved"
        else:
            registration_state = "unknown"

    return CapabilitiesOut(
        permissions=permissions,
        org=OrgSummaryOut(
            id=ctx.org.id,
            name=ctx.org.name,
            slug=ctx.org.slug,
            has_provider=has_provider,
            has_number=has_number,
            member_count=int(member_count),
            registration_state=registration_state,
        ),
    )


@router.get("/notifications", response_model=NotificationsOut)
async def list_my_notifications(
    ctx: Annotated[OrgContext, Depends(get_current_org)],
    unread: bool = False,
    limit: int = Query(50, ge=1, le=200),
) -> NotificationsOut:
    # Deliberately NOT require_permission: these read only the caller's OWN bell rows,
    # and every role (including agent) must be able to see its own bell.
    if ctx.actor_user_id is None:
        return NotificationsOut(items=[], unread_count=0)

    notifications = await notifications_svc.list_for_user(
        ctx.session,
        ctx.org.id,
        ctx.actor_user_id,
        unread_only=unread,
        limit=limit,
    )
    unread_count = await notifications_svc.unread_count(
        ctx.session, ctx.org.id, ctx.actor_user_id
    )

    # One batched lookup for the (our_e164, contact_e164) pair of every notification's
    # thread, so the bell can navigate straight to a conversation - it is addressed by
    # that number pair, not by thread id.
    thread_ids = {n.thread_id for n in notifications if n.thread_id is not None}
    pairs_by_thread = await notifications_svc.thread_pairs_by_id(ctx.session, thread_ids)

    return NotificationsOut(
        items=[
            NotificationOut(
                id=notification.id,
                kind=notification.kind,
                thread_id=notification.thread_id,
                our_e164=pairs_by_thread.get(notification.thread_id, (None, None))[0],
                contact_e164=pairs_by_thread.get(notification.thread_id, (None, None))[1],
                body=notification.body,
                read_at=notification.read_at,
                created_at=notification.created_at,
            )
            for notification in notifications
        ],
        unread_count=unread_count,
    )


@router.post("/notifications/read")
async def read_my_notifications(
    payload: NotificationsReadIn,
    ctx: Annotated[OrgContext, Depends(get_current_org)],
) -> dict:
    if ctx.actor_user_id is None:
        return {"updated": 0}

    if not payload.all_ and not payload.ids:
        raise ValidationFailedError("Choose which notifications to mark as read.")

    updated = await notifications_svc.mark_read(
        ctx.session,
        ctx.org.id,
        ctx.actor_user_id,
        ids=payload.ids,
        all_=payload.all_,
    )
    await ctx.session.commit()
    return {"updated": updated}
