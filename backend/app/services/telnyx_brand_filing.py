"""Operator-driven filing of a local Brand with Telnyx (10DLC).

A FUTURE, explicit platform-operator action - nothing here runs on its own, and no
network call happens unless an operator (or a test injecting a mock transport) calls it.
It maps a local ``Brand`` to a Telnyx payload, submits it exactly once, and records the
carrier's ``brandId``.

A brand submission is non-refundable, so these rules make the single POST safe:

* **Fileable local states.** Filing proceeds only while the local brand is ``draft`` or
  ``submitted`` (an unset status counts as draft) AND has no recorded Telnyx ``brandId``
  and no prior Telnyx attempt marker. A terminal ``approved``/``rejected`` brand is
  refused outright, because the operator ``/status`` route can set those without any
  Telnyx registration - POSTing a fresh Telnyx brand against a locally-approved row would
  leave a brand that is filed-but-unapproved at Telnyx masquerading as approved locally.
* **The ``submitted`` compromise.** The legacy ``/brands/{id}/submit`` route is
  deliberately a LOCAL-ONLY "ready to file" action: it sets ``Brand.status`` to
  ``submitted`` BEFORE any carrier filing, and the UI workflow depends on that. This
  service therefore treats ``submitted``-without-a-ref as ready-to-file rather than
  refusing it. On a 2xx the local status stays ``submitted`` and only the carrier ref is
  added, so a locally-submitted brand still cannot send - sending eligibility requires the
  carrier ``approved`` status (``can_send``). The gap between local ``submitted`` and
  carrier ``approved`` is preserved: they are never made to compare equal.
* **Durably mark the attempt before the POST.** A committed "pending" marker in
  ``Brand.carrier_refs`` means a retry after a timeout - or a second operator clicking
  twice - is refused instead of creating a duplicate registration. Any attempt marker or
  carrier ref refuses a repeat, regardless of status.
* **A timeout is not a failure.** On any error the existing local status is preserved and
  the marker is left in place (the carrier may already have accepted the request); a human
  must reconcile against Telnyx. The marker is what distinguishes "attempted, outcome
  unknown" from "never filed".

``carrier_refs`` (one brand can live at several carriers) uses two keys for Telnyx:
``"telnyx"`` for the carrier ``brandId`` string, and ``"telnyx_brand_filing"`` for the
pending-attempt marker dict. No secret (EIN, bearer key, full request body, raw carrier
reply) is logged or placed in an exception message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import (
    ConflictError,
    FeatureUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.numbers import TERMINAL_REGISTRATION, Brand
from app.providers.telnyx.brand_payload import build_brand_payload
from app.providers.telnyx.registration import TelnyxRegistrationClient
from app.services import provider_accounts
from app.services.registration import advance_status, validate_brand_for_submission

log = structlog.get_logger("telnyx_brand_filing")

#: carrier_refs key holding the Telnyx ``brandId`` string once filing succeeds.
_BRAND_ID_KEY = "telnyx"
#: carrier_refs key holding the durable "attempted, outcome unknown" marker.
_ATTEMPT_KEY = "telnyx_brand_filing"

#: Local states from which a Telnyx brand may be filed. ``submitted`` is the legacy
#: local-only "ready to file" state set by POST /brands/{id}/submit; ``draft`` is the
#: untouched starting state.
_FILEABLE_STATUSES = frozenset({"draft", "submitted"})


def _secret(value: Any) -> str:
    """Read a SecretStr (or plain) value without ever str()-ing a masked one."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return getter()
    return "" if value is None else str(value)


def _carrier_refs(brand: Brand) -> dict:
    """A copy of the brand's carrier refs, so writes always assign a NEW dict (the JSON
    column does not track in-place mutation)."""
    refs = brand.carrier_refs
    return dict(refs) if isinstance(refs, dict) else {}


def _carrier_brand_id(brand: Brand) -> str | None:
    value = _carrier_refs(brand).get(_BRAND_ID_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _attempt_marker(brand: Brand) -> dict | None:
    value = _carrier_refs(brand).get(_ATTEMPT_KEY)
    return value if isinstance(value, dict) and value else None


def _require_fileable_status(brand: Brand) -> None:
    """Fail closed BEFORE any marker write or POST unless the brand is in a local state we
    are allowed to file.

    The local status is not a faithful mirror of Telnyx: an operator can set ``approved``
    via the ``/status`` route with no Telnyx registration behind it, and the legacy
    ``/brands/{id}/submit`` route sets ``submitted`` as a local "ready to file" signal
    before any carrier filing. So we accept ``draft`` and ``submitted`` (the repeat guard
    separately refuses those that already carry a Telnyx ref or marker), and refuse the
    terminal states.
    """
    status = brand.status or "draft"
    if status in TERMINAL_REGISTRATION:
        raise ConflictError(
            f"Brand is already {status}; filing a Telnyx brand is refused because the "
            "local status is terminal and this service only files brands that are not "
            "yet decided"
        )
    if status not in _FILEABLE_STATUSES:
        raise ConflictError(
            f"Brand status {status!r} is not fileable; expected one of "
            f"{sorted(_FILEABLE_STATUSES)}"
        )


async def _lock_brand(session: AsyncSession, brand: Brand) -> Brand:
    """Re-read the brand under a row lock so two concurrent filings cannot both pass the
    idempotency check.

    Production relies on Postgres ``SELECT ... FOR UPDATE`` here for that serialization.
    SQLite (used by the offline tests) ignores ``FOR UPDATE``, so the row lock is a
    production-only guarantee; under SQLite the committed attempt marker - written before
    the POST - is the backstop, and callers must not treat the lock as sufficient there.
    """
    locked = (
        await session.execute(
            sa.select(Brand)
            .where(Brand.id == brand.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError("Brand not found")
    return locked


async def _resolve_telnyx_settings(session: AsyncSession, settings: Settings) -> object:
    """The org's ACTIVE Telnyx account laid over the base settings when there is one,
    else the base settings (a platform-operator run against a globally configured key).
    The caller still has to supply a usable key either way."""
    account = await provider_accounts.active_account_for(session, "telnyx")
    if account is None:
        return settings
    return provider_accounts.settings_like_for(settings, account)


async def file_brand_with_telnyx(
    session: AsyncSession,
    settings: Settings,
    brand: Brand,
    *,
    company_name: Any,
    first_name: Any,
    last_name: Any,
    brand_relationship: Any,
    mobile_phone: Any = None,
    client: httpx.AsyncClient | None = None,
) -> Brand:
    """Submit ``brand`` to Telnyx exactly once and record the carrier ``brandId``.

    ``client`` is an injected httpx client (e.g. a ``MockTransport`` in tests); when
    omitted the transport owns and closes its own client, and no live call is ever made
    unless an operator invokes this for real.
    """
    brand = await _lock_brand(session, brand)
    brand_id = str(brand.id)

    # Fail closed on a terminal local status BEFORE we touch the carrier or write a marker
    # - see _require_fileable_status. This is the guard that keeps a status set by the
    # operator /status route from being contradicted by a fresh, unapproved Telnyx brand.
    _require_fileable_status(brand)

    # Validate everything else we can before writing any marker: the carrier would reject
    # these days later, and a rejection costs real time.
    validate_brand_for_submission(brand)

    # Repeat guard: any prior attempt marker OR recorded carrier ref refuses a re-file,
    # whatever the (fileable) local status is. This is what makes the legacy `submitted`
    # state safe to accept - it is only accepted with no ref and no marker.
    if _attempt_marker(brand) is not None or _carrier_brand_id(brand) is not None:
        log.warning("telnyx_brand_filing_refused_repeat", brand_id=brand_id)
        raise ConflictError(
            "A Telnyx brand filing was already attempted for this brand; reconcile the "
            "existing filing before retrying"
        )

    payload = build_brand_payload(
        brand,
        company_name=company_name,
        first_name=first_name,
        last_name=last_name,
        brand_relationship=brand_relationship,
        mobile_phone=mobile_phone,
    )

    resolved = await _resolve_telnyx_settings(session, settings)
    api_key = _secret(getattr(resolved, "telnyx_api_key", None)).strip()
    if not api_key:
        raise ValidationFailedError(
            "No Telnyx API key is available; add and verify an active Telnyx account (or "
            "configure one) before filing a brand"
        )

    # Persist the pending marker and COMMIT it before the POST. This is what stops a
    # timeout/retry (or a second click) from submitting the same non-refundable brand
    # twice: the next caller sees the marker and is refused.
    attempt_id = str(uuid.uuid4())
    refs = _carrier_refs(brand)
    refs[_ATTEMPT_KEY] = {
        "status": "pending",
        "attempt_id": attempt_id,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    brand.carrier_refs = refs
    await session.commit()
    # The commit expires the instance; reload so later attribute reads never trigger a
    # lazy load (illegal under async) and reflect the now-durable marker.
    await session.refresh(brand)

    registration = TelnyxRegistrationClient(api_key=api_key, client=client)
    try:
        result = await registration.create_brand(payload)
    except Exception as exc:
        # Leave the existing local status and the marker untouched: the outcome may be
        # unknown (a timeout can arrive after Telnyx accepted the POST), so the marker
        # records "attempted, outcome unknown" and only a human reconciling against Telnyx
        # may clear it. Never log the payload, the key or the carrier body.
        log.warning(
            "telnyx_brand_filing_failed",
            brand_id=brand_id,
            attempt_id=attempt_id,
            error=type(exc).__name__,
        )
        raise
    finally:
        # No-op when the caller injected the client (we do not own it).
        await registration.aclose()

    telnyx_brand_id = result.get("brandId") or result.get("id")
    if not telnyx_brand_id:
        # A 2xx without an identifier is not a usable success; the marker stays pending.
        raise FeatureUnavailableError("Telnyx returned no brand identifier in its response")

    filed = _carrier_refs(brand)
    filed[_BRAND_ID_KEY] = str(telnyx_brand_id)
    filed.pop(_ATTEMPT_KEY, None)
    brand.carrier_refs = filed
    # Status is earned, and never regresses: advance_status is monotonic, so a draft brand
    # moves to submitted and a brand already in the legacy `submitted` state stays there -
    # the carrier ref is what now records the filing. A locally-submitted brand is not
    # sendable until the carrier reports `approved`; the two never compare equal.
    advance_status(brand, "submitted")
    await session.commit()

    log.info("telnyx_brand_filed", brand_id=brand_id, attempt_id=attempt_id)
    return brand
