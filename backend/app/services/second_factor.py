"""Second factor is mandatory for privileged accounts, optional for everyone else.

A second factor used to be required for every account in the product. It is now required only
for users who hold a privileged role in ANY org they belong to - as of SYSTEM_ROLES that is
"owner" (wildcard) and "admin", i.e. any role granting ``*``, ``members:update`` or
``roles:write`` - and optional for ordinary staff ("agent"), who may enrol one but are not
obliged to.

"Privileged" is a property of the permission set, not of the role's name: a role is privileged
when it carries the wildcard or any of the privileged permissions. The check runs over every
membership the user has, so being an agent in one workspace does not cancel being an admin in
another - privileged in ANY org counts, and one admin membership obliges a factor for the whole
account.

The answer is DERIVED LIVE from the user's current roles on every call. It is never stored on
the user row and never cached. A flag stamped at signup would outlive a later promotion: an
agent who becomes an admin would keep signing in with a password alone, and nothing in the
product would look wrong. That is a privilege escalation wearing the costume of a preference,
so we recompute instead of remembering.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Role, User
from app.models.rbac import is_privileged_permissions


def required_from_roles(settings: Settings, roles: Iterable[Role]) -> bool:
    """True when the platform requires 2FA and any of ``roles`` is privileged.

    Callers that already hold the user's roles (``/auth/me``, the org picker) use this to
    answer the policy question without a second query. A user with no memberships has no
    privileged role, so this is False: a factor stays optional for them.
    """
    if not settings.require_2fa_privileged_users:
        return False
    return any(is_privileged_permissions(role.permissions) for role in roles)


async def requires_second_factor(
    session: AsyncSession, settings: Settings, user: User
) -> bool:
    """Is this account obliged to hold a second factor at all?

    Loads the user's roles across every org and delegates to ``required_from_roles``. It is
    deliberately independent of whether the account currently HAS a factor: the "you may not
    remove your last factor" guards need to know the obligation, not the current state.
    """
    if not settings.require_2fa_privileged_users:
        return False
    from app.repositories.orgs import list_memberships_for_user

    memberships = await list_memberships_for_user(session, user.id)
    return required_from_roles(settings, [role for _org, role in memberships])


async def must_enrol(session: AsyncSession, settings: Settings, user: User) -> bool:
    """Is this account obliged to hold a factor and currently missing one?

    This is what blocks requests and what the console reads. The early return is load-bearing,
    not a micro-optimisation: this runs on every authenticated request, so accounts that
    already hold a factor answer without a database round trip and only factorless accounts
    ever pay for the membership lookup. Do not "tidy" it away or reorder it after the query.
    """
    if user.has_second_factor:
        return False
    return await requires_second_factor(session, settings, user)
