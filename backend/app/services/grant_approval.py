"""P44d insider control: large free credit needs a SECOND operator.

An admin operator can hand any workspace money (console adjust) or message/minute bundles
(console bundle grant). One person doing that alone - a compromised operator login, or an
insider topping up a friend - is the classic free-credit fraud. So:

  credit    positive adjustments by one operator above CONSOLE_CREDIT_SOLO_MICROS in a UTC
            day ($50 by default) are not applied; they become a pending approval
  bundles   a grant larger than one bundle of that kind is not applied either

A pending approval is a SecurityAlert (kind "grant_pending") - no new table. A DIFFERENT
admin approves it and only then does the money move. Debits are never held back.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, NotFoundError, PermissionDeniedError

PENDING_KIND = "grant_pending"


def _midnight() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def credit_granted_today(session: AsyncSession, operator_user_id: uuid.UUID) -> int:
    """Positive console credit this operator applied today, across every workspace."""
    from app.models import CreditLedgerEntry

    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CreditLedgerEntry.amount_micros), 0))
            .where(
                CreditLedgerEntry.created_by == operator_user_id,
                CreditLedgerEntry.entry_type == "adjustment",
                CreditLedgerEntry.amount_micros > 0,
                CreditLedgerEntry.reference.like("console:%"),
                CreditLedgerEntry.created_at >= _midnight(),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    return int(total or 0)


async def needs_second_operator_for_credit(
    session: AsyncSession,
    settings,  # noqa: ANN001
    operator_user_id: uuid.UUID,
    amount_micros: int,
) -> bool:
    if amount_micros <= 0:
        return False
    solo = int(getattr(settings, "console_credit_solo_micros", 50_000_000))
    return await credit_granted_today(session, operator_user_id) + amount_micros > solo


def needs_second_operator_for_bundle(kind: str, units: int) -> bool:
    from app.services import bundles

    return units > bundles.UNITS_PER_BUNDLE.get(kind, 0)


async def request(
    session: AsyncSession, org_id: uuid.UUID, requested_by: uuid.UUID, grant: dict
) -> uuid.UUID:
    from app.models import SecurityAlert

    row = SecurityAlert(
        id=uuid.uuid4(),
        kind=PENDING_KIND,
        org_id=org_id,
        detail={**grant, "requested_by": str(requested_by)},
    )
    session.add(row)
    await session.flush()
    return row.id


async def pending(session: AsyncSession) -> list:
    from app.models import SecurityAlert

    return list(
        (
            await session.execute(
                sa.select(SecurityAlert)
                .where(SecurityAlert.kind == PENDING_KIND, SecurityAlert.status == "open")
                .order_by(SecurityAlert.created_at)
            )
        ).scalars()
    )


async def decide(
    session: AsyncSession, alert_id: uuid.UUID, approver_id: uuid.UUID, *, approve: bool
) -> dict:
    """Approve (apply) or reject a pending grant. The approver must not be the requester."""
    from app.models import SecurityAlert
    from app.services import bundles, credits

    row = await session.get(SecurityAlert, alert_id)
    if row is None or row.kind != PENDING_KIND:
        raise NotFoundError("Pending grant not found")
    if row.status != "open":
        raise ConflictError("This grant was already decided")
    detail = dict(row.detail or {})
    if detail.get("requested_by") == str(approver_id):
        raise PermissionDeniedError(
            "A second operator must approve this - you requested it.",
            code="second_operator_required",
        )
    # Claim the decision atomically: two operators clicking at once must not both apply
    # it. The losing UPDATE waits for the winner's commit, then matches no row.
    status = "resolved" if approve else "dismissed"
    reviewed_at = datetime.now(timezone.utc)
    claimed = await session.execute(
        sa.update(SecurityAlert)
        .where(SecurityAlert.id == alert_id, SecurityAlert.status == "open")
        .values(status=status, reviewed_by=approver_id, reviewed_at=reviewed_at)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ConflictError("This grant was already decided")
    row.status, row.reviewed_by, row.reviewed_at = status, approver_id, reviewed_at
    result: dict = {"status": row.status}
    if approve:
        org_id = row.org_id
        set_org_context(session, org_id)
        note = f"{detail.get('note', '')} (approved by a second operator)"[:255]
        if detail.get("type") == "credit":
            entry = await credits.adjust(
                session,
                org_id,
                int(detail["amount_micros"]),
                reference=f"console-grant:{alert_id}",
                note=note,
                created_by=uuid.UUID(detail["requested_by"]),
            )
            result["balance_after_micros"] = int(entry.balance_after_micros)
        elif detail.get("type") == "bundle":
            entry = await bundles.credit(
                session,
                org_id,
                detail["kind"],
                int(detail["units"]),
                reference=f"console-grant:{alert_id}",
                note=note,
                entry_type="adjustment",
            )
            result["units_after"] = int(entry.balance_after_units)
    return result
