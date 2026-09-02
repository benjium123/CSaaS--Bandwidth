"""Sweeper hook for polling asynchronous number orders.

Bandwidth orders return `RECEIVED`, not `COMPLETE`. Until the carrier says COMPLETE the
number is not routable, so we keep an `org_numbers.status == "pending"` row and poll here
until it either becomes active or fails.

Bounded to `limit` polled orders per pass and committed per row. A failure for one carrier
must never break the pass; it is logged and the row remains pending for a later attempt.
"""

from __future__ import annotations

import structlog

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import OrgNumber
from app.providers import registry_org
from app.services import credentials as credential_svc

log = structlog.get_logger("sweeper.number_orders")


def _pollable_carrier_names(carriers: object) -> list[str]:
    """Provider names whose adapter (as resolved by `carriers` RIGHT NOW - which, for a
    `CarrierRegistryProxy`, depends on `registry_org.CURRENT_ORG_ID` already being set to
    the org being processed) exposes the optional `order_status` coroutine."""
    names_fn = getattr(carriers, "names", None)
    get_fn = getattr(carriers, "get", None)
    if names_fn is None or get_fn is None:
        return []
    names: list[str] = []
    for name in names_fn():
        provider = get_fn(name)
        if provider is not None and hasattr(provider, "order_status"):
            names.append(name)
    return names


async def poll_pending_number_orders(
    session: AsyncSession, carriers: object, *, limit: int = 25, settings: object | None = None
) -> int:
    """Poll pending number orders, org by org, committing each row.

    The sweeper passes the carrier registry (`app.state.carriers`) and, when it has one,
    `app.state.settings` - needed to re-prime a DB-backed org's carrier registry (see the
    priming block below). Only carriers whose adapter has the optional `order_status`
    method are polled - filtered in SQL (never a Python-side skip) so a run of unpollable
    rows ahead of a pollable one can never starve it out of the per-pass `limit`.
    """
    if carriers is None or limit <= 0:
        return 0

    # List orgs first without tenant context (this query deliberately crosses tenants only
    # to enumerate them; actual row processing below is org-scoped).
    org_ids = (
        await session.execute(
            sa.select(OrgNumber.org_id)
            .where(OrgNumber.status == "pending", OrgNumber.provider_ref.is_not(None))
            .distinct()
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()

    polled = 0
    for org_id in org_ids:
        if polled >= limit:
            break

        # Establish org context for every select/update in this pass AND for the carrier
        # registry proxy: CarrierRegistryProxy._resolve() (app/providers/registry_org.py)
        # only returns a DB-backed adapter when CURRENT_ORG_ID names the org whose primed
        # cache entry it is - without setting it here, polling would silently fall back to
        # the env-configured carrier (or None) for every org that provisioned via a P17 DB
        # account. ONE try/finally spans both the query and the row loop below, so a
        # failing org (query exception, mid-loop exception, anything) still always resets
        # both contexts before the next org - never leaves a context bound past this org's
        # iteration.
        set_org_context(session, org_id)
        org_token = registry_org.CURRENT_ORG_ID.set(org_id)
        try:
            # P18 review: CarrierRegistryProxy._resolve() only returns a DB-backed
            # adapter for an org whose per-org cache entry is present AND still inside
            # the TTL backstop (registry_org.is_primed) - a request naturally re-primes
            # that cache on every authenticated hit, but the sweeper has no request, so
            # a DB-backed carrier would silently fall back to the env carrier (or None)
            # once the TTL lapsed since anyone last used that org. Prime it here too,
            # the exact same way app/auth/deps.py::get_current_org does: cheap-path
            # gated on the master key being configured at all, and skipped entirely
            # when already primed.
            if (
                settings is not None
                and credential_svc.master_key_present(settings)
                and not registry_org.is_primed(org_id)
            ):
                try:
                    global_registry = getattr(carriers, "global_registry", None)
                    await registry_org.prime_org_registry(
                        session, settings, org_id, global_registry=global_registry
                    )
                except Exception:
                    log.exception("number_order_org_prime_failed", org_id=str(org_id))

            try:
                pollable_names = _pollable_carrier_names(carriers)
            except Exception:
                log.exception("number_order_carrier_lookup_failed", org_id=str(org_id))
                continue
            if not pollable_names:
                # Nothing this org's registry can poll - no point querying rows for it.
                continue

            try:
                rows = list(
                    (
                        await session.execute(
                            sa.select(OrgNumber)
                            .where(
                                OrgNumber.status == "pending",
                                OrgNumber.provider_ref.is_not(None),
                                OrgNumber.carrier.in_(pollable_names),
                            )
                            .order_by(OrgNumber.e164)
                            .limit(limit - polled)
                        )
                    )
                    .scalars()
                    .all()
                )
            except Exception:
                log.exception("number_order_org_query_failed", org_id=str(org_id))
                continue

            for number in rows:
                if polled >= limit:
                    break

                provider = getattr(carriers, "get", lambda _name: None)(number.carrier)
                if provider is None or not hasattr(provider, "order_status"):
                    # The SQL filter above already restricts rows to pollable carrier
                    # names, but the carrier registry can change between that filter and
                    # here (a concurrent credential change) - re-check rather than trust
                    # a name that is no longer resolvable.
                    continue

                polled += 1
                try:
                    result = await provider.order_status(number.provider_ref)
                except Exception:
                    log.exception(
                        "number_order_poll_failed",
                        e164=number.e164,
                        provider_ref=number.provider_ref,
                    )
                    # Do not raise out of the loop: one bad carrier response must not kill
                    # the whole sweeper pass.
                    continue

                status = getattr(result, "status", None)
                detail = getattr(result, "detail", None)

                if status == "active":
                    number.status = "active"
                    number.is_active = True
                    number.order_detail = None
                elif status == "failed":
                    number.status = "failed"
                    number.is_active = False
                    number.order_detail = (
                        str(detail) if detail else "Carrier order failed"
                    )[:512]
                else:
                    # Still pending (or unknown). Keep the row pending and retain the raw
                    # carrier status for the operator until a later sweep resolves it.
                    raw = str(status or "pending")
                    number.order_detail = (
                        str(detail) if detail else raw
                    )[:512]

                try:
                    await session.commit()
                except Exception:
                    log.exception(
                        "number_order_commit_failed",
                        e164=number.e164,
                        provider_ref=number.provider_ref,
                    )
                    await session.rollback()
                    continue
        finally:
            registry_org.CURRENT_ORG_ID.reset(org_token)
            set_org_context(session, None)

    return polled
