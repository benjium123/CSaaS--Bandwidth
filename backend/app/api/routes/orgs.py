from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Imported so there is exactly ONE cross-org inbox check in the codebase: inboxes.py does
# NOT import orgs.py, so this alias is not an import cycle.
from app.api.routes.inboxes import _get_inbox as _get_inbox_for_org
from app.auth.deps import (
    OrgContext,
    check_org_selfie_step_up,
    get_current_user,
    require_permission,
)
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import WILDCARD, InboxGrant, Invite, OrgMembership, Role, User
from app.models.rbac import is_privileged_permissions
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo
from app.services import account_security, contact_visibility, password_policy
from app.services import audit as audit_svc
from app.services import calling_settings as calling_settings_svc
from app.services import defaults as defaults_svc
from app.services import invites as invites_svc
from app.services import kyc as kyc_svc
from app.services import retention as retention_svc
from app.services import seats as seats_svc

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


class OrgCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    permissions: list[str]
    is_system: bool


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role_name: str


class RetentionOut(BaseModel):
    messages_days: int | None
    recordings_days: int | None
    transcripts_days: int | None
    imports_days: int | None


class RetentionPatch(BaseModel):
    messages_days: int | None = None
    recordings_days: int | None = None
    transcripts_days: int | None = None
    imports_days: int | None = None


class OrgSettingsIn(BaseModel):
    contact_visibility: str = Field(min_length=1, max_length=16)


class CallingSettingsIn(BaseModel):
    recording_announcement: bool | None = None
    recording_announcement_text: str | None = None
    channel_layout: str | None = None
    dispositions: list[str] | None = None


def _privileged_grant_action(role: Role) -> str | None:
    perms = set(role.permissions or [])
    if WILDCARD in perms:
        return "ownership_transfer"
    if perms & {"org:billing", "members:update", "roles:write"}:
        return "admin_grant"
    return None


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    payload: OrgCreateIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrgOut:
    settings = request.app.state.settings
    # P41: one workspace in verification at a time - applying for many businesses at once
    # is how a banned operator probes which identity gets through.
    if settings.kyc_enforced and await kyc_svc.unverified_orgs_owned_by(session, user.id):
        raise ConflictError(
            "Finish verifying your current business before creating another workspace",
            code="kyc_pending_elsewhere",
        )
    org = await orgs_repo.create_org_with_owner(session, name=payload.name, owner_id=user.id)
    org.account_type = "individual"
    org.number_subscription_required = True
    await session.flush()
    await defaults_svc.seed_org_defaults(session, org.id, owner_user_id=user.id)
    # P41: every new workspace starts unverified; telephony waits for approval.
    set_org_context(session, org.id)
    await kyc_svc.get_or_create_profile(session, org.id)
    from app.services import billing_ops

    await billing_ops.grant_welcome_credit(session, org.id)
    await session.commit()
    return OrgOut(id=org.id, name=org.name, slug=org.slug)


@router.get("/current", response_model=OrgOut)
async def current_org(
    ctx: Annotated[OrgContext, Depends(require_permission("org:read"))],
) -> OrgOut:
    return OrgOut(id=ctx.org.id, name=ctx.org.name, slug=ctx.org.slug)


@router.get("/current/settings")
async def current_org_settings(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> dict:
    return {"contact_visibility": ctx.org.contact_visibility}


@router.patch("/current/settings")
async def update_org_settings(
    payload: OrgSettingsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict:
    if payload.contact_visibility not in contact_visibility.POLICIES:
        raise ValidationFailedError(
            "Contact visibility must be everyone, department or owner"
        )
    ctx.org.contact_visibility = payload.contact_visibility
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="org.settings_update",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"contact_visibility": payload.contact_visibility},
    )
    await ctx.session.commit()
    return {"contact_visibility": ctx.org.contact_visibility}


@router.get("/current/calling")
async def current_calling(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> dict:
    return calling_settings_svc.as_dict(ctx.org)


@router.patch("/current/calling")
async def update_calling_settings(
    payload: CallingSettingsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict:
    changed: list[str] = []

    if "recording_announcement" in payload.model_fields_set:
        if payload.recording_announcement is None:
            raise ValidationFailedError("Recording announcement must be true or false")
        ctx.org.recording_announcement = payload.recording_announcement
        changed.append("recording_announcement")

    if "recording_announcement_text" in payload.model_fields_set:
        ctx.org.recording_announcement_text = (
            calling_settings_svc.normalize_announcement_text(
                payload.recording_announcement_text
            )
        )
        changed.append("recording_announcement_text")

    normalized_dispositions = None
    normalized_channel_layout = None

    if "dispositions" in payload.model_fields_set:
        normalized_dispositions = calling_settings_svc.normalize_dispositions(
            payload.dispositions
        )
        changed.append("dispositions")

    if "channel_layout" in payload.model_fields_set:
        normalized_channel_layout = calling_settings_svc.normalize_channel_layout(
            payload.channel_layout
        )
        changed.append("channel_layout")

    if normalized_dispositions is not None or normalized_channel_layout is not None:
        calling_settings_svc.apply(
            ctx.org,
            dispositions=normalized_dispositions,
            channel_layout=normalized_channel_layout,
        )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="org.calling_settings_update",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"fields": changed},
    )
    await ctx.session.commit()
    return calling_settings_svc.as_dict(ctx.org)


@router.get("/current/retention", response_model=RetentionOut)
async def current_retention(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> RetentionOut:
    """The four "how long do we keep this" numbers. Reading them creates the row with the
    defaults the Settings copy promises, so the page never has to explain a missing row."""
    policy = await retention_svc.get_or_create_policy(ctx.session, ctx.org.id)
    await ctx.session.commit()
    return RetentionOut(
        messages_days=policy.messages_days,
        recordings_days=policy.recordings_days,
        transcripts_days=policy.transcripts_days,
        imports_days=policy.imports_days,
    )


@router.patch("/current/retention", response_model=RetentionOut)
async def update_retention(
    payload: RetentionPatch,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> RetentionOut:
    """`exclude_unset` matters here: "left alone" and "cleared, so keep it forever" are
    different answers and must not collapse into one."""
    updates = payload.model_dump(exclude_unset=True)
    policy = await retention_svc.update_policy(ctx.session, ctx.org.id, updates)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="retention.update",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail=updates,
    )
    await ctx.session.commit()
    return RetentionOut(
        messages_days=policy.messages_days,
        recordings_days=policy.recordings_days,
        transcripts_days=policy.transcripts_days,
        imports_days=policy.imports_days,
    )


@router.post("/current/seed-defaults")
async def seed_defaults(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict:
    summary = await defaults_svc.seed_org_defaults(
        ctx.session,
        ctx.org.id,
        owner_user_id=ctx.actor_user_id,
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="org.seed_defaults",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail=summary,
    )
    await ctx.session.commit()
    return summary


@router.get("/current/roles", response_model=list[RoleOut])
async def current_org_roles(
    ctx: Annotated[OrgContext, Depends(require_permission("roles:read"))],
) -> list[RoleOut]:
    """The resource the tenancy gate test reads across tenants.

    Note there is no ``.where(org_id == ...)`` here. That is deliberate: the session-level
    guard injects it. If the guard ever regresses, this endpoint leaks — which is exactly
    why the gate test asserts on it.
    """
    result = await ctx.session.execute(sa.select(Role))
    return [
        RoleOut(id=r.id, name=r.name, permissions=r.permissions or [], is_system=r.is_system)
        for r in result.scalars().all()
    ]


@router.get("/current/seats")
async def current_org_seats(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
) -> dict:
    """User seats: one per paid, unreleased phone number, the owner included."""
    return (await seats_svc.usage(ctx.session, ctx.org.id)).public()


@router.get("/current/members", response_model=list[MemberOut])
async def current_org_members(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
) -> list[MemberOut]:
    stmt = (
        sa.select(OrgMembership, User, Role)
        .join(User, User.id == OrgMembership.user_id)
        .join(Role, Role.id == OrgMembership.role_id)
    )
    rows = (await ctx.session.execute(stmt)).all()
    return [
        MemberOut(
            user_id=u.id, email=u.email, full_name=u.full_name, role_name=r.name
        )
        for _m, u, r in rows
    ]


class MemberUpdateIn(BaseModel):
    role_name: str = Field(min_length=1, max_length=64)


class MemberCreateIn(BaseModel):
    email: EmailStr
    full_name: str = Field(default="", max_length=255)
    password: str = Field(min_length=1, max_length=256)
    #: "admin" or "agent". Never "owner" - see services/invites.INVITABLE_ROLES.
    role_name: str = "agent"
    #: OPTIONAL. Inboxes to grant the new user, role "member", in the SAME transaction
    #: that creates the account. Empty/absent => byte-identical behaviour to before.
    inbox_ids: list[uuid.UUID] = []


async def _get_role_for_org(ctx: OrgContext, role_name: str) -> Role:
    role = (
        await ctx.session.execute(
            sa.select(Role).where(Role.org_id == ctx.org.id, Role.name == role_name)
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"Role {role_name!r} does not exist in this organisation")
    return role


def _role_assignable_by(actor_role: Role, target_role: Role) -> bool:
    """C1: an actor may only ever set (update_member) or act on (remove_member) a role
    whose permission set is a SUBSET of their own - never grant, or remove someone
    holding, a role with capabilities the actor itself lacks. Without this, an admin
    (members:update/members:remove but no wildcard) could self-promote to owner, or
    remove an owner outright. An actor holding the wildcard is exempt - it dominates
    every other role's permission set by definition."""
    actor_perms = actor_role.permissions or []
    if WILDCARD in actor_perms:
        return True
    target_perms = set(target_role.permissions or [])
    return target_perms.issubset(set(actor_perms))


@router.post("/current/members", response_model=MemberOut, status_code=201)
async def create_member(
    payload: MemberCreateIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:invite"))],
) -> MemberOut:
    """Create a member account directly, with the admin choosing the first credential.

    This exists BESIDE the invite flow, never instead of it. On the production box SMTP is
    not configured, so an emailed accept-invite link can never be delivered and the
    invitation-only path means nobody can be added at all. An admin who can reach the
    person out of band can set up the account here and hand the credential over directly.
    The invite flow is untouched and keeps working exactly as before.

    Owner is refused here for the SAME reason invites refuse it: an account that can delete
    the workspace and see the billing is transferred deliberately, never minted as a side
    effect of an email address plus a role string. That is why the role is checked against
    ``invites.INVITABLE_ROLES`` (which excludes "owner") before anything is written, and
    why containment mirrors ``create_invite`` - a bare ``members:invite`` holder must not be
    able to mint themselves an admin account.

    No forced first-login rotation is implemented, and ``password_changed_at`` is left NULL
    exactly as ``create_user`` leaves it: there is no ``must_change_password`` column and a
    migration is out of scope. The admin knows the credential, and the
    ``account.created_by_admin`` audit row is the record of that. Forcing a rotation needs a
    real column (a future decision), because the only migration-free signal we have
    (``password_changed_at IS NULL``) is also NULL for every pre-existing account and would
    drag the entire user base into a rotation prompt.

    Transaction boundary: every row this handler writes - the ``User``, the
    ``OrgMembership`` and every ``InboxGrant`` - is committed by the SINGLE
    ``await ctx.session.commit()`` at the very end. There is no nested transaction and no
    intermediate commit, so any raised error discards the whole thing. That is what makes
    "create teammate + grant their number(s)" atomic: either the teammate exists with their
    number(s), or nothing was written at all. The grants are built only AFTER every inbox id
    has been validated against the caller's org.
    """
    # The rule that makes "owner" impossible - before any row is written.
    if payload.role_name not in invites_svc.INVITABLE_ROLES:
        raise ValidationFailedError(
            f"Role must be one of: {', '.join(invites_svc.INVITABLE_ROLES)}. "
            "Ownership is transferred deliberately, never by invitation."
        )

    new_role = await _get_role_for_org(ctx, payload.role_name)

    # C1: without this a holder of only members:invite could mint themselves an admin.
    if not _role_assignable_by(ctx.role, new_role):
        raise PermissionDeniedError(
            "You cannot create a member with a role that has more permissions than your own."
        )

    # P41: handing out owner or admin/billing power needs a fresh selfie from the grantor.
    if _privileged_grant_action(new_role) is not None:
        await check_org_selfie_step_up(request, ctx, action="admin_grant")

    # The policy service owns the length/strength rule; let its own message reach the admin.
    await password_policy.check(
        request.app.state.settings, payload.password, email=payload.email
    )

    # ---- OPTIONAL inbox grants, folded into the SAME transaction as the account. ----
    # Everything here happens BEFORE create_user, so a bad inbox id leaves no user row.
    granted_inbox_ids: list[uuid.UUID] = []
    if payload.inbox_ids:
        # (a) Permission gate. This route is gated on members:invite; without this check a
        #     bare members:invite holder could hand out inbox access it cannot otherwise
        #     grant through PUT /api/v1/inboxes/assignments (which requires inboxes:admin).
        #     Role.grants already short-circuits the wildcard.
        if not ctx.role.grants("inboxes:admin"):
            raise PermissionDeniedError(
                "Granting inboxes requires permission: inboxes:admin"
            )
        # (b) De-duplicate preserving order. Repeating an id is NOT an error; it must not
        #     trip the uq_inbox_grants_inbox_grantee unique constraint.
        granted_inbox_ids = list(dict.fromkeys(payload.inbox_ids))
        # (c) Validate EVERY id belongs to the caller's org before anything is written,
        #     reusing the EXISTING check rather than writing a second one.
        #     _get_inbox_for_org is the ONE cross-org inbox check (imported at module top).
        #     Inbox is TenantScoped, so a foreign id resolves to nothing and it raises
        #     NotFoundError (404) - the whole request is rejected on the FIRST bad id.
        for inbox_id in granted_inbox_ids:
            await _get_inbox_for_org(ctx, inbox_id)

    # Each paid number buys one user; checked before any row is written.
    await seats_svc.require_seat(ctx.session, ctx.org.id)

    # create_user hashes the password itself and raises ConflictError (409) on a duplicate.
    user = await users_repo.create_user(
        ctx.session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
    )

    # The session already carries this org's tenant context (require_permission set it) and
    # the row's org_id equals it, so the before_flush guard accepts it - no allow-unscoped
    # escape hatch here, and it would only widen the blast radius.
    ctx.session.add(
        OrgMembership(
            id=uuid.uuid4(),
            org_id=ctx.org.id,
            user_id=user.id,
            role_id=new_role.id,
        )
    )
    await ctx.session.flush()

    # The grants themselves. grantee_type is ALWAYS "user" here: this code must never write
    # or delete a "department" row - a department's grant on the same inbox belongs to a
    # whole team and is none of this handler's business.
    for inbox_id in granted_inbox_ids:
        ctx.session.add(
            InboxGrant(
                id=uuid.uuid4(),
                org_id=ctx.org.id,
                inbox_id=inbox_id,
                grantee_type="user",
                grantee_id=user.id,
                role="member",
            )
        )

    # TWO audit rows, and NEITHER carries the password. ``credential_set_by_admin`` records
    # WHY this matters: the credential was chosen by someone else. These rows are what makes
    # "who gave this person this number" answerable later. The ``inbox_ids`` key is added
    # ONLY when there are grants, so the empty case is byte-identical to before.
    member_detail: dict = {
        "email": user.email,
        "role_name": new_role.name,
        "credential_set_by_admin": True,
    }
    account_detail: dict = {"org_id": str(ctx.org.id), "role_name": new_role.name}
    if granted_inbox_ids:
        granted_ids = [str(i) for i in granted_inbox_ids]
        member_detail["inbox_ids"] = granted_ids
        account_detail["inbox_ids"] = granted_ids
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="member.created",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail=member_detail,
    )
    account_security.audit(
        ctx.session,
        user.id,
        "account.created_by_admin",
        actor_user_id=ctx.actor_user_id,
        request=request,
        detail=account_detail,
    )

    # ONE transaction: the single commit at the very end, after every validation has passed.
    await ctx.session.commit()
    return MemberOut(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role_name=new_role.name,
    )


@router.delete("/current/members/{user_id}", status_code=204)
async def remove_member(
    user_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:remove"))],
) -> Response:
    row = (
        await ctx.session.execute(
            sa.select(OrgMembership, Role)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == ctx.org.id, OrgMembership.user_id == user_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Member not found")
    membership, current_role = row

    if not _role_assignable_by(ctx.role, current_role):
        raise PermissionDeniedError("You cannot remove a member with a higher role than your own")

    if current_role.name == "owner":
        owner_count = (
            await ctx.session.execute(
                sa.select(sa.func.count())
                .select_from(OrgMembership)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == ctx.org.id, Role.name == "owner")
            )
        ).scalar_one()
        if owner_count <= 1:
            raise ConflictError("Cannot remove the last owner of an organisation")

    await ctx.session.delete(membership)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        action="member.removed",
        target_type="org_membership",
        target_id=str(user_id),
        detail={"user_id": str(user_id)},
    )
    # P42: leaving a workspace ends every session - a removed person must not keep a live
    # login (which could still reach other workspaces' data they are about to lose too).
    revoked = await account_security.revoke_sessions(
        ctx.session, request.app.state.settings, user_id, revoked_by=ctx.actor_user_id or user_id
    )
    await ctx.session.commit()
    await account_security.mark_revoked(request.app.state.settings, revoked)
    return Response(status_code=204)


@router.patch("/current/members/{user_id}", response_model=MemberOut)
async def update_member(
    user_id: uuid.UUID,
    payload: MemberUpdateIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:update"))],
) -> MemberOut:
    row = (
        await ctx.session.execute(
            sa.select(OrgMembership, User, Role)
            .join(User, User.id == OrgMembership.user_id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == ctx.org.id, OrgMembership.user_id == user_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Member not found")
    membership, user, current_role = row

    # E1: an admin (no wildcard) could otherwise demote a peer OWNER by only ever
    # having the TARGET role checked below - assigning e.g. "agent" (a subset of
    # admin's own permissions) passed even though the MEMBER being changed outranks
    # the actor. Same subset rule remove_member already applies to current_role.
    if not _role_assignable_by(ctx.role, current_role):
        raise PermissionDeniedError(
            "You cannot change the role of a member with a higher role than your own"
        )

    new_role = await _get_role_for_org(ctx, payload.role_name)

    if not _role_assignable_by(ctx.role, new_role):
        raise PermissionDeniedError(
            "You cannot assign a role with more permissions than your own"
        )
    # P41: handing out owner or admin/billing power needs a fresh selfie from the grantor.
    action = _privileged_grant_action(new_role)
    if action is not None and new_role.id != current_role.id:
        await check_org_selfie_step_up(request, ctx, action=action)

    if current_role.name == "owner" and new_role.name != "owner":
        owner_count = (
            await ctx.session.execute(
                sa.select(sa.func.count())
                .select_from(OrgMembership)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == ctx.org.id, Role.name == "owner")
            )
        ).scalar_one()
        if owner_count <= 1:
            raise ConflictError("Cannot demote the last owner of an organisation")

    if user_id == ctx.actor_user_id:
        raise PermissionDeniedError("You cannot change your own role")

    membership.role_id = new_role.id
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        action="member.updated",
        target_type="org_membership",
        target_id=str(user_id),
        detail={"user_id": str(user_id), "role_name": new_role.name},
    )
    # P42: a role change takes effect on a fresh sign-in, never on a session minted under
    # the old role.
    revoked = await account_security.revoke_sessions(
        ctx.session, request.app.state.settings, user_id, revoked_by=ctx.actor_user_id or user_id
    )
    await ctx.session.commit()
    await account_security.mark_revoked(request.app.state.settings, revoked)
    set_org_context(ctx.session, ctx.org.id)
    return MemberOut(
        user_id=user.id, email=user.email, full_name=user.full_name, role_name=new_role.name
    )


# ----------------------------------------------------------------------------------
# Invitations - the only way in, once the instance has an owner
# ----------------------------------------------------------------------------------
class InviteIn(BaseModel):
    email: EmailStr
    #: "admin" or "agent". Never "owner" - see services/invites.INVITABLE_ROLES.
    role_name: str = "agent"


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role_name: str
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InviteCreatedOut(InviteOut):
    #: Shown EXACTLY ONCE. Only a hash is stored, so this cannot be recovered later -
    #: if it is lost, revoke the invite and issue a new one.
    token: str
    #: Ready-to-send link, built from PUBLIC_BASE_URL.
    accept_url: str


def _invite_out(inv: Invite) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role_name=inv.role_name,
        expires_at=inv.expires_at,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
    )


@router.get("/current/invites", response_model=list[InviteOut])
async def list_invites(
    ctx: Annotated[OrgContext, Depends(require_permission("members:read"))],
) -> list[InviteOut]:
    """Outstanding and historical invitations. Never includes a token."""
    rows = (
        await ctx.session.execute(sa.select(Invite).order_by(Invite.created_at.desc()))
    ).scalars().all()
    return [_invite_out(i) for i in rows]


@router.post("/current/invites", response_model=InviteCreatedOut, status_code=201)
async def create_invite(
    payload: InviteIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:invite"))],
) -> InviteCreatedOut:
    invited_role = (
        await ctx.session.execute(
            sa.select(Role).where(Role.org_id == ctx.org.id, Role.name == payload.role_name)
        )
    ).scalar_one_or_none()
    # C1 (audit): the SAME containment rule update_member applies. Without it a member
    # holding only members:invite could invite a second address of their own AS ADMIN and
    # operate from it - escalation by proxy, with update_member's own guard bypassed. The
    # selfie step-up below is not a substitute: it proves WHO is asking, never that they may
    # grant this role, and it is skipped entirely when KYC_ENFORCED is off.
    # Consequence, deliberately accepted: a role may only invite what it holds itself, so a
    # narrow "inviter" role must carry the permissions it hands out (e.g. agent's + invite).
    if invited_role is not None and not _role_assignable_by(ctx.role, invited_role):
        raise PermissionDeniedError(
            "You cannot invite someone to a role with more permissions than your own. "
            "Ask an owner or admin to send this invitation, or to add those permissions "
            "to your role."
        )
    if invited_role is not None and _privileged_grant_action(invited_role) is not None:
        await check_org_selfie_step_up(request, ctx, action="admin_grant")
    invite, raw = await invites_svc.create_invite(
        ctx.session,
        org_id=ctx.org.id,
        email=payload.email,
        role_name=payload.role_name,
        created_by=ctx.actor_user_id,
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="invite.created",
        target_type="invite",
        target_id=str(invite.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"email": payload.email, "role_name": payload.role_name},
    )
    await ctx.session.commit()
    settings = request.app.state.settings
    base = (settings.public_web_url or settings.public_base_url or "").rstrip("/")
    accept_url = f"{base}/accept-invite?token={raw}"
    # P42: the invitation goes straight to the invited address, so the link never has to
    # pass through chat or a shared inbox.
    from app.services import mailer

    await mailer.send(
        settings,
        [payload.email],
        f"You're invited to {ctx.org.name} on {settings.app_name}",
        f"You were invited to join {ctx.org.name} as {payload.role_name}.\n\n"
        f"Accept the invitation (the link works once and expires in 7 days):\n{accept_url}",
    )
    return InviteCreatedOut(
        **_invite_out(invite).model_dump(),
        token=raw,
        accept_url=accept_url,
    )


@router.delete("/current/invites/{invite_id}", response_model=InviteOut)
async def revoke_invite(
    invite_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("members:invite"))],
) -> InviteOut:
    invite = await ctx.session.get(Invite, invite_id)
    if invite is None:
        raise NotFoundError("Invitation not found")
    if invite.accepted_at is not None:
        raise ConflictError("That invitation has already been used")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="invite.revoked",
            target_type="invite",
            target_id=str(invite.id),
            actor_user_id=ctx.actor_user_id,
            actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        )
        await ctx.session.commit()
    return _invite_out(invite)


# ----------------------------------------------------------------------------------
# P42: admin reset of a member's sign-in factors, and deactivation
# ----------------------------------------------------------------------------------
def _is_privileged(role: Role) -> bool:
    # Canonical definition lives in models/rbac.py; this wrapper keeps the local call sites.
    return is_privileged_permissions(role.permissions)


async def _resettable_member(ctx: OrgContext, user_id: uuid.UUID) -> User:
    """A member whose factors this workspace may reset: not privileged, not an operator, and
    a member of THIS workspace only. An account is global, so resetting someone who also
    works elsewhere would let one workspace weaken another's security."""
    from app.db.base import ALLOW_UNSCOPED_KEY
    from app.services import operators as operators_svc

    if ctx.membership is None:
        raise PermissionDeniedError("A signed-in person must do this")
    if user_id == ctx.membership.user_id:
        raise PermissionDeniedError("Use account recovery for your own account")
    # JUSTIFIED allow_unscoped: must see the target's memberships in EVERY workspace.
    rows = (
        await ctx.session.execute(
            sa.select(OrgMembership, Role)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.user_id == user_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    set_org_context(ctx.session, ctx.org.id)
    if not any(m.org_id == ctx.org.id for m, _ in rows):
        raise NotFoundError("Member not found")
    if len(rows) > 1:
        raise PermissionDeniedError(
            "This person also belongs to another workspace. Ask them to use account recovery.",
            code="member_in_other_workspace",
        )
    if _is_privileged(rows[0][1]):
        raise PermissionDeniedError(
            "Owners, admins and billing members recover their own account with an ID check.",
            code="privileged_member",
        )
    if await operators_svc.is_operator(ctx.session, user_id):
        raise PermissionDeniedError("Platform operators cannot be reset from a workspace")
    target = await ctx.session.get(User, user_id)
    if target is None:
        raise NotFoundError("Member not found")
    return target


@router.post("/current/members/{user_id}/reset-2fa", status_code=204)
async def reset_member_factors(
    user_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("members:update"))],
) -> Response:
    from app.auth.deps import check_step_up

    settings = request.app.state.settings
    actor = await ctx.session.get(User, ctx.membership.user_id) if ctx.membership else None
    if actor is None:
        raise PermissionDeniedError("A signed-in person must do this")
    await check_step_up(request, ctx.session, actor, kind="recent_2fa", action="member_reset")
    set_org_context(ctx.session, ctx.org.id)
    target = await _resettable_member(ctx, user_id)
    cleared = await account_security.clear_second_factors(ctx.session, target)
    revoked = await account_security.revoke_sessions(
        ctx.session, settings, target.id, revoked_by=actor.id
    )
    account_security.audit(
        ctx.session, target.id, "factors.reset_by_admin", actor_user_id=actor.id, request=request,
        detail={**cleared, "org_id": str(ctx.org.id)},
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="member.factors_reset",
        target_type="user",
        target_id=str(target.id),
        actor_user_id=actor.id,
        detail=cleared,
    )
    await ctx.session.commit()
    await account_security.mark_revoked(settings, revoked)
    await account_security.notify_now(
        settings,
        target.email,
        "Your sign-in methods were reset",
        f"An admin of {ctx.org.name} reset your passkeys and authenticator app. Sign in with "
        "your password and set up a new passkey or authenticator app.",
    )
    return Response(status_code=204)
