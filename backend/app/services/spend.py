from __future__ import annotations

import calendar
import math
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ValidationFailedError
from app.models import (
    DEFAULT_RATES_MICROS,
    SPEND_METRICS,
    TRAFFIC_SCOPE,
    Call,
    Message,
    Org,
    OrgNumber,
    ProviderRate,
    ProviderSpendDaily,
)
from app.services.usage import NON_BILLABLE_OUTBOUND_STATUSES

#: Upper bound on an org's own rate override (Opus review item 9) - a fat-fingered
#: unit_cost_micros (e.g. dollars typed into a micros field) must fail loudly, not
#: silently multiply every future rollup's cost by a million.
MAX_UNIT_COST_MICROS = 10_000_000_000


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return UTC [start, end) bounds for a calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _ceil_minutes(duration_seconds: int | None) -> int:
    """Billing minutes for one call: ceil per call, 0-or-None-duration contributes 0.

    ``rollup_day`` computes this in SQL now (Opus review item 7: ``ceil(cast(duration,
    float) / 60.0)``, grouped and summed server-side) - this stays as the equivalent
    pure-Python reference. ``math.ceil`` of any duration in (0, 60] is already >= 1, so
    the old ``max(1, ...)`` wrap was unreachable and is dropped (Opus review item 9).
    """
    if duration_seconds is None or duration_seconds <= 0:
        return 0
    return math.ceil(duration_seconds / 60)


async def resolve_rate(
    session: AsyncSession, provider: str, metric: str
) -> tuple[int, bool, bool]:
    """Return (unit_cost_micros, is_override, is_known) for one provider metric.

    An org override row wins when present; otherwise the code-level default applies.
    ``is_known`` is False only when the provider has no catalogue entry at all (a
    carrier we have never priced) - every metric of every catalogued provider always
    has a default, so a False here means "no rate card", not merely "no override".
    """
    row = (
        await session.execute(
            sa.select(ProviderRate.unit_cost_micros).where(
                ProviderRate.provider == provider,
                ProviderRate.metric == metric,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return int(row), True, True

    default = DEFAULT_RATES_MICROS.get(provider, {}).get(metric)
    if default is None:
        return 0, False, False
    return int(default), False, True


async def effective_rates(session: AsyncSession) -> list[dict]:
    """Every (provider, metric) in the default catalogue with override flag."""
    overrides = {
        (r.provider, r.metric): int(r.unit_cost_micros)
        for r in (await session.execute(sa.select(ProviderRate))).scalars().all()
    }

    out: list[dict] = []
    for provider in DEFAULT_RATES_MICROS:
        for metric in SPEND_METRICS:
            default_cost = int(DEFAULT_RATES_MICROS[provider].get(metric, 0))
            if (provider, metric) in overrides:
                cost = overrides[(provider, metric)]
                is_override = True
            else:
                cost = default_cost
                is_override = False
            out.append(
                {
                    "provider": provider,
                    "metric": metric,
                    "unit_cost_micros": cost,
                    "default_unit_cost_micros": default_cost,
                    "is_override": is_override,
                    "currency": "USD",
                }
            )
    return out


async def upsert_rates(
    session: AsyncSession,
    items: list[dict],
    *,
    org_id: uuid.UUID,
) -> list[ProviderRate]:
    """Validate and upsert org rate overrides. Rows are unique per
    ``(org_id, provider, metric)``. ``org_id`` is always the authenticated caller's
    org - there is no unscoped fallback (Opus review item 9: a rate write with no
    resolvable tenant must fail, never guess)."""
    if not items:
        return []

    seen: set[tuple[str, str]] = set()
    validated: list[dict] = []
    for item in items:
        provider = item.get("provider")
        metric = item.get("metric")
        cost = item.get("unit_cost_micros")

        if not isinstance(provider, str) or not isinstance(metric, str) or not isinstance(cost, int):
            raise ValidationFailedError("provider, metric, and unit_cost_micros are required")
        if provider not in DEFAULT_RATES_MICROS:
            raise ValidationFailedError(f"Unknown provider: {provider}")
        if metric not in SPEND_METRICS:
            raise ValidationFailedError(f"Unknown metric: {metric}")
        if cost < 0:
            raise ValidationFailedError("unit_cost_micros must be >= 0")
        if cost > MAX_UNIT_COST_MICROS:
            raise ValidationFailedError(
                f"unit_cost_micros must not exceed {MAX_UNIT_COST_MICROS}"
            )
        if (provider, metric) in seen:
            raise ValidationFailedError("Duplicate provider/metric pair in rates payload")
        seen.add((provider, metric))
        validated.append(
            {"provider": provider, "metric": metric, "unit_cost_micros": int(cost)}
        )

    upserted: list[ProviderRate] = []
    for item in validated:
        existing = (
            await session.execute(
                sa.select(ProviderRate).where(
                    ProviderRate.org_id == org_id,
                    ProviderRate.provider == item["provider"],
                    ProviderRate.metric == item["metric"],
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            row = ProviderRate(
                id=uuid.uuid4(),
                org_id=org_id,
                provider=item["provider"],
                metric=item["metric"],
                unit_cost_micros=item["unit_cost_micros"],
                currency="USD",
            )
            session.add(row)
        else:
            row = existing
            row.unit_cost_micros = item["unit_cost_micros"]

        await session.flush()
        upserted.append(row)

    return upserted


async def rollup_day(session: AsyncSession, org_id: uuid.UUID, day: date) -> int:
    """Recompute one org/UTC day of provider spend and replace that day's rows.

    The delete + insert happens in a single transaction and commits exactly once.
    Returns the number of ``provider_spend_daily`` rows written.
    """
    set_org_context(session, org_id)
    start, end = _day_bounds(day)
    days_in_month = calendar.monthrange(day.year, day.month)[1]

    await session.execute(
        sa.delete(ProviderSpendDaily).where(
            ProviderSpendDaily.org_id == org_id,
            ProviderSpendDaily.period_date == day,
        )
    )

    rows_to_add: list[ProviderSpendDaily] = []

    # --- Message traffic (aggregated in SQL) --------------------------------------
    # Quantity is the carrier-reported segment count when known, else our own estimate,
    # else 1 (mirrors usage.py's sms_segments billing rule) - a carrier bills segments,
    # not messages. is_mms is a portable "does this message have any media" check: CAST
    # to TEXT and compare against the empty-array literal works identically for SQLite's
    # JSON-as-TEXT storage and Postgres JSONB's canonical (whitespace-free) text output,
    # with no per-dialect branch needed. Only OUTBOUND messages a carrier could
    # plausibly have billed count - queued (still held) and rejected (blocked before it
    # ever reached the carrier) are excluded, same as usage.py; inbound is never gated
    # by our own send-pipeline statuses.
    qty_expr = sa.func.coalesce(Message.segment_count_carrier, Message.segment_count_est, 1)
    is_mms_case = sa.case((sa.cast(Message.media, sa.Text) != "[]", 1), else_=0)
    traffic_stmt = (
        sa.select(
            Message.carrier,
            Message.direction,
            is_mms_case.label("is_mms"),
            sa.func.sum(qty_expr).label("quantity"),
        )
        .where(
            Message.org_id == org_id,
            Message.created_at >= start,
            Message.created_at < end,
            sa.or_(
                Message.direction != "outbound",
                Message.status.not_in(NON_BILLABLE_OUTBOUND_STATUSES),
            ),
        )
        .group_by(Message.carrier, Message.direction, is_mms_case)
    )
    for carrier, direction, is_mms, quantity in (await session.execute(traffic_stmt)).all():
        quantity = int(quantity or 0)
        if quantity <= 0:
            continue
        if direction == "outbound":
            metric = "mms_out" if is_mms else "sms_out"
        else:
            metric = "mms_in" if is_mms else "sms_in"

        rate, _is_override, _is_known = await resolve_rate(session, carrier, metric)
        rows_to_add.append(
            ProviderSpendDaily(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=day,
                provider=carrier,
                metric=metric,
                quantity=quantity,
                cost_micros=quantity * rate,
                number_id=None,
                scope_key=TRAFFIC_SCOPE,
            )
        )

    # --- Voice traffic (aggregated in SQL) ----------------------------------------
    # ceil(duration_seconds / 60) PER CALL, summed - the cast to float is required
    # because duration_seconds is an integer column and integer division would
    # truncate the fraction ceil() needs (61/60 as integer division is 1, not 1.0166).
    # A 0-or-NULL-duration call is excluded outright, contributing nothing.
    minutes_expr = sa.func.ceil(sa.cast(Call.duration_seconds, sa.Float) / 60.0)
    voice_stmt = (
        sa.select(
            Call.carrier,
            Call.direction,
            sa.func.sum(minutes_expr).label("quantity"),
        )
        .where(
            Call.org_id == org_id,
            Call.created_at >= start,
            Call.created_at < end,
            Call.duration_seconds.isnot(None),
            Call.duration_seconds > 0,
        )
        .group_by(Call.carrier, Call.direction)
    )
    for carrier, direction, quantity in (await session.execute(voice_stmt)).all():
        quantity = int(round(quantity or 0))
        if quantity <= 0:
            continue
        metric = "voice_min_out" if direction == "outbound" else "voice_min_in"
        rate, _is_override, _is_known = await resolve_rate(session, carrier, metric)
        rows_to_add.append(
            ProviderSpendDaily(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=day,
                provider=carrier,
                metric=metric,
                quantity=quantity,
                cost_micros=quantity * rate,
                number_id=None,
                scope_key=TRAFFIC_SCOPE,
            )
        )

    # --- Number MRC: numbers that EXISTED that day --------------------------------
    # "Existed that day" = purchased on or before this day (purchased_at is NULL for
    # pre-P18 numbers with no recorded purchase, which always count) AND not released
    # before this day started. status != "pending" (not == "active") so a released
    # number still gets its past days' MRC on a re-rollup - a release must never erase
    # history, only stop future accrual (the released_at >= start clause already does
    # that: once released_at falls before a day's start, that day is excluded).
    mrc_stmt = sa.select(
        OrgNumber.id,
        OrgNumber.carrier,
        OrgNumber.monthly_cost_cents,
    ).where(
        OrgNumber.org_id == org_id,
        OrgNumber.status != "pending",
        sa.or_(OrgNumber.purchased_at.is_(None), OrgNumber.purchased_at < end),
        sa.or_(OrgNumber.released_at.is_(None), OrgNumber.released_at >= start),
    )
    for number_id, carrier, monthly_cost_cents in (await session.execute(mrc_stmt)).all():
        rate, _is_override, _is_known = await resolve_rate(session, carrier, "number_mrc")
        cost_micros = (
            (monthly_cost_cents * 10_000) if monthly_cost_cents is not None else rate
        ) // days_in_month
        rows_to_add.append(
            ProviderSpendDaily(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=day,
                provider=carrier,
                metric="number_mrc",
                quantity=1,
                cost_micros=int(cost_micros),
                number_id=number_id,
                scope_key=str(number_id),
            )
        )

    # --- Number setup on purchase day only ----------------------------------------
    setup_stmt = sa.select(
        OrgNumber.id,
        OrgNumber.carrier,
        OrgNumber.purchase_cost_cents,
    ).where(
        OrgNumber.org_id == org_id,
        OrgNumber.purchased_at >= start,
        OrgNumber.purchased_at < end,
    )
    for number_id, carrier, purchase_cost_cents in (await session.execute(setup_stmt)).all():
        rate, _is_override, _is_known = await resolve_rate(session, carrier, "number_setup")
        cost_micros = purchase_cost_cents * 10_000 if purchase_cost_cents is not None else rate
        rows_to_add.append(
            ProviderSpendDaily(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=day,
                provider=carrier,
                metric="number_setup",
                quantity=1,
                cost_micros=int(cost_micros),
                number_id=number_id,
                scope_key=str(number_id),
            )
        )

    if rows_to_add:
        session.add_all(rows_to_add)
        await session.flush()

    await session.commit()
    return len(rows_to_add)


async def rollup_recent(session: AsyncSession, *, days: int = 2) -> int:
    """Sweeper hook: roll up today plus the previous ``days - 1`` UTC days for every org.

    Mirrors usage.py's org enumeration and commit-per-org discipline. Returns number
    of orgs rolled.
    """
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )

    today = datetime.now(timezone.utc).date()
    for oid in org_ids:
        for offset in range(days):
            await rollup_day(session, oid, today - timedelta(days=offset))

    return len(org_ids)


async def summary(session: AsyncSession, org_id: uuid.UUID, start: date, end: date) -> dict:
    """Aggregate provider spend for a date range, joined to number e164s."""
    stmt = (
        sa.select(
            ProviderSpendDaily.provider,
            ProviderSpendDaily.metric,
            ProviderSpendDaily.number_id,
            OrgNumber.e164,
            sa.func.sum(ProviderSpendDaily.quantity).label("quantity"),
            sa.func.sum(ProviderSpendDaily.cost_micros).label("cost_micros"),
        )
        .outerjoin(OrgNumber, ProviderSpendDaily.number_id == OrgNumber.id)
        .where(
            ProviderSpendDaily.org_id == org_id,
            ProviderSpendDaily.period_date >= start,
            ProviderSpendDaily.period_date <= end,
        )
        .group_by(
            ProviderSpendDaily.provider,
            ProviderSpendDaily.metric,
            ProviderSpendDaily.number_id,
            OrgNumber.e164,
        )
    )

    rows = (await session.execute(stmt)).all()

    by_provider: dict[str, dict] = {}
    number_by_provider: dict[str, dict[uuid.UUID, dict]] = {}
    total_micros = 0

    for provider, metric, number_id, e164, quantity, cost in rows:
        quantity = int(quantity or 0)
        cost = int(cost or 0)
        total_micros += cost

        p = by_provider.setdefault(
            provider, {"cost_micros": 0, "by_metric": {}, "numbers": []}
        )
        p["cost_micros"] += cost

        m = p["by_metric"].setdefault(metric, {"quantity": 0, "cost_micros": 0})
        m["quantity"] += quantity
        m["cost_micros"] += cost

        if number_id is not None:
            nmap = number_by_provider.setdefault(provider, {})
            n = nmap.setdefault(
                number_id,
                {"number_id": str(number_id), "e164": e164, "cost_micros": 0},
            )
            n["cost_micros"] += cost

    for provider, nmap in number_by_provider.items():
        by_provider[provider]["numbers"] = list(nmap.values())

    # A provider absent from DEFAULT_RATES_MICROS has no rate card at all - an org
    # override cannot exist for it either (upsert_rates rejects unknown providers), so
    # catalogue membership alone determines "unrated" (Opus review item 6).
    unrated_providers = sorted(p for p in by_provider if p not in DEFAULT_RATES_MICROS)

    return {
        "total_micros": total_micros,
        "by_provider": by_provider,
        "unrated_providers": unrated_providers,
    }


async def daily(
    session: AsyncSession,
    org_id: uuid.UUID,
    start: date,
    end: date,
    provider: str | None = None,
) -> list[dict]:
    """Flat per-day rows for charts and reconciliation."""
    stmt = sa.select(ProviderSpendDaily).where(
        ProviderSpendDaily.org_id == org_id,
        ProviderSpendDaily.period_date >= start,
        ProviderSpendDaily.period_date <= end,
    )
    if provider is not None:
        stmt = stmt.where(ProviderSpendDaily.provider == provider)

    stmt = stmt.order_by(
        ProviderSpendDaily.period_date,
        ProviderSpendDaily.provider,
        ProviderSpendDaily.metric,
    )

    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "period_date": row.period_date,
            "provider": row.provider,
            "metric": row.metric,
            "quantity": int(row.quantity),
            "cost_micros": int(row.cost_micros),
        }
        for row in rows
    ]


async def month_to_date_micros(session: AsyncSession, org_id: uuid.UUID, today: date) -> int:
    """Micros spent from the first of ``today``'s month through ``today`` inclusive."""
    start = date(today.year, today.month, 1)
    value = (
        await session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(ProviderSpendDaily.cost_micros), 0)
            ).where(
                ProviderSpendDaily.org_id == org_id,
                ProviderSpendDaily.period_date >= start,
                ProviderSpendDaily.period_date <= today,
            )
        )
    ).scalar_one()
    return int(value)
