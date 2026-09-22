"""Named-admin customer directory, selective bans and guarded permanent deletion."""

from __future__ import annotations

import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, Base
from app.errors import NotFoundError, ValidationFailedError
from app.models import (
    KycPerson,
    KycProfile,
    NumberPurchase,
    Org,
    OrgMembership,
    OrgNumber,
    PlatformOperator,
    Role,
    SecurityAlert,
    User,
)
from app.models.subscriptions import Subscription
from app.services import account_security, ban_list


# JUSTIFIED: these helpers are only called from named platform-admin routes; accounts span tenants.
def unscoped(statement):
    return statement.execution_options(**{ALLOW_UNSCOPED_KEY: True})


async def memberships(session, user_id):
    return (
        await session.execute(
            unscoped(
                sa.select(Org, Role.name, KycProfile.status)
                .join(OrgMembership, OrgMembership.org_id == Org.id)
                .join(Role, Role.id == OrgMembership.role_id)
                .outerjoin(KycProfile, KycProfile.org_id == Org.id)
                .where(OrgMembership.user_id == user_id)
            )
        )
    ).all()


async def directory(session, q, offset, limit):
    query = sa.select(User)
    if q:
        query = query.where(
            sa.or_(
                User.email.icontains(q, autoescape=True),
                User.full_name.icontains(q, autoescape=True),
            )
        )
    total = await session.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
    users = (
        (
            await session.execute(
                query.order_by(User.created_at.desc(), User.id).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    accounts = []
    for user in users:
        orgs = await memberships(session, user.id)
        operator = await session.scalar(
            sa.select(PlatformOperator.id).where(PlatformOperator.user_id == user.id)
        )
        accounts.append(
            {
                "id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "created_at": user.created_at.isoformat(),
                "is_active": user.is_active,
                "email_verified": not user.email_verification_required
                or user.email_verified_at is not None,
                "is_operator": operator is not None,
                "workspaces": [
                    {
                        "id": str(org.id),
                        "name": org.name,
                        "account_type": org.account_type,
                        "role": role,
                        "status": status or "not_started",
                    }
                    for org, role, status in orgs
                ],
            }
        )
    return {"accounts": accounts, "total": total, "offset": offset, "limit": limit}


async def target(session, user_id, actor_id):
    user = await session.scalar(sa.select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise NotFoundError("Account not found")
    if user.id == actor_id or await session.scalar(
        sa.select(PlatformOperator.id).where(PlatformOperator.user_id == user.id)
    ):
        raise ValidationFailedError("Platform administrator accounts are protected")
    return user


async def identifiers(session, user):
    values = [{"kind": "email", "value": user.email, "label": user.email}]
    orgs = await memberships(session, user.id)
    for org, role, _ in orgs:
        if role != "owner":
            continue
        profile = await session.scalar(
            unscoped(sa.select(KycProfile).where(KycProfile.org_id == org.id))
        )
        if profile:
            applicant = (profile.use_case or {}).get("applicant_details") or {}
            for phone in (profile.business_phone, applicant.get("phone")):
                if phone:
                    values.append({"kind": "phone", "value": phone, "label": phone})
    people = (
        await session.execute(
            unscoped(
                sa.select(KycPerson).where(
                    KycPerson.user_id == user.id,
                    KycPerson.identity_provider == "didit",
                    KycPerson.identity_hash.is_not(None),
                )
            )
        )
    ).scalars()
    for person in people:
        values.append(
            {
                "kind": "person",
                "hash": person.identity_hash,
                "label": f"Didit verified identity: {person.verified_name or person.full_name}",
            }
        )
    unique = {}
    for item in values:
        digest = item.get("hash") or ban_list.hash_value(item["kind"], item["value"])
        key = f"{item['kind']}:{digest}"
        unique[key] = {**item, "key": key, "hash": digest}
    return list(unique.values())


async def deletion_plan(session, user):
    orgs = await memberships(session, user.id)
    delete_orgs, blockers = [], []
    for org, role, _ in orgs:
        await session.execute(sa.select(Org.id).where(Org.id == org.id).with_for_update())
        count = await session.scalar(
            unscoped(
                sa.select(sa.func.count())
                .select_from(OrgMembership)
                .where(OrgMembership.org_id == org.id)
            )
        )
        if count == 1:
            delete_orgs.append(org)
        elif role == "owner":
            blockers.append(
                f"{org.name}: transfer ownership or remove other members "
                "before deleting this owner."
            )
    for org in delete_orgs:
        numbers = await session.scalar(
            unscoped(
                sa.select(sa.func.count())
                .select_from(OrgNumber)
                .where(OrgNumber.org_id == org.id, OrgNumber.status != "released")
            )
        )
        subs = await session.scalar(
            unscoped(
                sa.select(sa.func.count())
                .select_from(Subscription)
                .where(
                    Subscription.org_id == org.id,
                    Subscription.status.not_in(["canceled", "incomplete_expired"]),
                )
            )
        )
        purchases = await session.scalar(
            unscoped(
                sa.select(sa.func.count())
                .select_from(NumberPurchase)
                .where(
                    NumberPurchase.org_id == org.id,
                    sa.or_(
                        NumberPurchase.state.not_in(["complete", "canceled", "expired"]),
                        sa.and_(
                            NumberPurchase.subscription_id.is_not(None),
                            sa.or_(
                                NumberPurchase.subscription_status.is_(None),
                                NumberPurchase.subscription_status.not_in(
                                    ["canceled", "incomplete_expired"]
                                ),
                            ),
                        ),
                    ),
                )
            )
        )
        if numbers or subs or purchases:
            blockers.append(
                f"{org.name}: release phone numbers and cancel subscriptions "
                "or pending number orders first."
            )
    return delete_orgs, blockers


async def apply_bans(session, user, actor_id, keys, reason):
    available = {v["key"]: v for v in await identifiers(session, user)}
    if any(key not in available for key in keys):
        raise ValidationFailedError(
            "An identifier changed. Reload the account before blacklisting."
        )
    for key in set(keys):
        item = available[key]
        await ban_list.add(
            session,
            kind=item["kind"],
            value_hash=item["hash"],
            reason=reason,
            created_by=actor_id,
            display_hint=ban_list.hint(item["kind"], item.get("value", "")),
        )
    return len(set(keys))


async def delete_account(session, request, user, actor_id, confirmation, keys, reason):
    if confirmation != user.email:
        raise ValidationFailedError("Type the account email exactly to confirm deletion")
    orgs, blockers = await deletion_plan(session, user)
    if blockers:
        raise ValidationFailedError(" ".join(blockers))
    await apply_bans(session, user, actor_id, keys, reason)
    revoked = await account_security.revoke_sessions(
        session, request.app.state.settings, user.id, revoked_by=actor_id
    )
    # Remove stored uploads before their DB metadata. Keep rows available on failure for retry.
    store = getattr(request.app.state, "media_store", None)
    for org in orgs:
        for table in Base.metadata.tables.values():
            if "org_id" not in table.c:
                continue
            for field in ("storage_key", "logo_key", "pdf_key"):
                if field not in table.c:
                    continue
                keys_to_delete = (
                    await session.execute(
                        sa.select(table.c[field]).where(
                            table.c.org_id == org.id, table.c[field].is_not(None)
                        )
                    )
                ).scalars()
                for key in keys_to_delete:
                    if key:
                        if store is None:
                            raise ValidationFailedError(
                                "File storage is unavailable; retry deletion when it is restored"
                            )
                        await store.delete(key)
        from app.models import ContactList
        from app.services.retention import _import_source_key

        lists = (
            await session.execute(
                unscoped(sa.select(ContactList.id).where(ContactList.org_id == org.id))
            )
        ).scalars()
        for list_id in lists:
            if store is None:
                raise ValidationFailedError("File storage is unavailable; retry deletion later")
            await store.delete(_import_source_key(org.id, list_id))
        # Explicitly remove tenant-owned records in FK order, including RESTRICT edges.
        # Global records with SET NULL (fraud identifiers, audit) are intentionally preserved.
        for table in reversed(Base.metadata.sorted_tables):
            if "org_id" in table.c and any(
                f.target_fullname == "orgs.id" and f.ondelete == "CASCADE"
                for f in table.c.org_id.foreign_keys
            ):
                await session.execute(sa.delete(table).where(table.c.org_id == org.id))
        await session.execute(sa.delete(Org).where(Org.id == org.id))
    user_id = str(user.id)
    await session.delete(user)
    # Global operator audit deliberately survives removal of the target user/workspace.
    session.add(
        SecurityAlert(
            kind="account_deleted",
            status="reviewed",
            reviewed_by=actor_id,
            detail={
                "target_user_id": user_id,
                "deleted_workspaces": [str(o.id) for o in orgs],
                "reason": reason,
                "blacklisted_identifiers": len(keys),
            },
        )
    )
    await session.commit()
    await account_security.mark_revoked(request.app.state.settings, revoked)
