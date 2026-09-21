"""Operator-driven filing of a local TollFreeVerification with Telnyx (TFV).

A FUTURE, explicit platform-operator action - nothing here runs on its own, and no network
call happens unless an operator (or a test injecting a mock transport) calls it. It maps a
local ``TollFreeVerification`` to a Telnyx toll-free verification request, submits it
exactly once, and records the carrier's request id.

TFV is the toll-free regime: no brand, no campaign, no TCR (see ``models/numbers.py``),
and it is deliberately NOT merged with the 10DLC brand/campaign machines. The rules below
make the single POST safe:

* **Terminal records are refused.** Filing proceeds only while the local verification is
  ``draft`` or ``submitted`` (an unset status counts as draft) AND has no recorded Telnyx
  request id and no prior Telnyx attempt marker. An ``approved`` or ``rejected`` record is
  refused outright: this operation only files records that are not yet decided.
* **The ``submitted`` compromise.** The legacy local "ready to file" workflow can set
  ``status`` to ``submitted`` BEFORE any carrier filing, so ``submitted``-without-a-ref is
  treated as ready-to-file rather than refused. On a 2xx the local status is advanced to
  ``submitted`` and only the carrier ref is added; a locally-submitted verification still
  cannot send, because sending eligibility requires the carrier ``approved`` status
  (``can_send``). The two never compare equal.
* **Tenancy, number relation and a usable carrier number.** The verification is locked and
  reloaded, the number it points at is locked and reloaded, and we require: same org on
  both rows, an actual toll-free number, a Telnyx-owned number, and that the number is
  still active (not released). A wrong carrier/number type would otherwise be filed
  against a number the carrier cannot verify.
* **Durably mark the attempt before the POST.** A committed "pending" marker in
  ``carrier_refs`` means a retry after a timeout - or a second operator clicking twice -
  is refused instead of creating a duplicate registration. Any attempt marker or carrier
  ref refuses a repeat, regardless of status.
* **A timeout is not a failure.** On any error the existing local status is preserved and
  the durable marker is left in place (the carrier may already have accepted the request).
  The marker distinguishes "attempted, outcome unknown" from "never filed"; while it is
  present, automatic retries are refused. An ambiguous create timeout may leave no Telnyx
  request id to look up at all, so resolving the case requires an explicit external
  reconciliation - a documented runbook or a Telnyx support investigation - before an
  operator deliberately repairs state. Nothing in this service clears the marker as part
  of a retry.
* **Never approve locally.** This service only ever advances the local status to
  ``submitted``; it never sets ``approved`` or ``rejected``. Approval is decided by the
  carrier and reflected only through the normal carrier-confirmed approval path.

``carrier_refs`` (one verification can live at several carriers) uses two keys for Telnyx:
``"telnyx"`` for the carrier request id string, and ``"telnyx_tfv_filing"`` for the
pending-attempt marker dict. No secret (bearer key, request body, business/contact data,
phone number or raw carrier reply) is logged or placed in an exception message.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
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
from app.models.messaging import OrgNumber
from app.models.numbers import TERMINAL_REGISTRATION, TollFreeVerification
from app.providers.telnyx.tollfree_payload import build_tollfree_verification_payload
from app.providers.telnyx.tollfree_verification import TelnyxTollfreeVerificationClient
from app.services import provider_accounts
from app.services.registration import advance_status

log = structlog.get_logger("telnyx_tollfree_filing")

#: carrier_refs key holding the carrier request id once filing succeeds.
_REQUEST_ID_KEY = "telnyx"
#: carrier_refs key holding the durable "attempted, outcome unknown" marker.
_ATTEMPT_KEY = "telnyx_tfv_filing"

#: Local states from which a Telnyx toll-free verification may be filed. ``submitted`` is
#: the legacy local-only "ready to file" state; ``draft`` is the untouched starting state.
_FILEABLE_STATUSES = frozenset({"draft", "submitted"})


def _secret(value: Any) -> str:
    """Read a SecretStr (or plain) value without ever str()-ing a masked one."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return getter()
    return "" if value is None else str(value)


def _carrier_refs(verification: TollFreeVerification) -> dict:
    """A copy of the verification's carrier refs, so writes always assign a NEW dict (the
    JSON column does not track in-place mutation)."""
    refs = verification.carrier_refs
    return dict(refs) if isinstance(refs, dict) else {}


def _carrier_request_id(verification: TollFreeVerification) -> str | None:
    value = _carrier_refs(verification).get(_REQUEST_ID_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _attempt_marker(verification: TollFreeVerification) -> dict | None:
    value = _carrier_refs(verification).get(_ATTEMPT_KEY)
    return value if isinstance(value, dict) and value else None


def _require_fileable_status(verification: TollFreeVerification) -> None:
    """Fail closed BEFORE any marker write or POST unless the verification is in a local
    state we are allowed to file.

    This operation refuses terminal records: an ``approved`` or ``rejected`` verification
    is not something this service may file. Non-terminal states outside
    ``draft``/``submitted`` are refused too (the repeat guard separately refuses those
    already carrying a Telnyx ref or marker).
    """
    status = verification.status or "draft"
    if status in TERMINAL_REGISTRATION:
        raise ConflictError(
            f"Toll-free verification is already {status}; filing a Telnyx verification is "
            "refused because this operation only files records that are not yet decided"
        )
    if status not in _FILEABLE_STATUSES:
        raise ConflictError(
            f"Toll-free verification status {status!r} is not fileable; expected one of "
            f"{sorted(_FILEABLE_STATUSES)}"
        )


def _require_usable_number(
    verification: TollFreeVerification, number: OrgNumber
) -> None:
    """The TFV may only be filed against the toll-free Telnyx number it belongs to, and
    only while that number is active. A released or foreign number cannot be verified."""
    if number.org_id != verification.org_id:
        raise ConflictError(
            "Toll-free verification and number belong to different orgs; refusing to file"
        )
    if (number.number_type or "") != "tollfree":
        raise ValidationFailedError(
            "Toll-free verification must be filed against a toll-free number"
        )
    if (number.carrier or "").strip().lower() != "telnyx":
        raise ValidationFailedError(
            "Toll-free verification can only be filed with Telnyx for a Telnyx number"
        )
    if not number.is_active or (number.status or "active") != "active":
        raise ConflictError(
            "The number is not active at the carrier; a released or pending number cannot "
            "be filed for toll-free verification"
        )


async def _lock_verification(
    session: AsyncSession, verification: TollFreeVerification
) -> TollFreeVerification:
    """Re-read the verification under a row lock so two concurrent filings cannot both pass
    the idempotency check.

    Production relies on Postgres ``SELECT ... FOR UPDATE`` here for that serialization.
    SQLite (used by the offline tests) ignores ``FOR UPDATE``, so the row lock is a
    production-only guarantee; under SQLite the committed attempt marker - written before
    the POST - is the backstop, and callers must not treat the lock as sufficient there.
    """
    locked = (
        await session.execute(
            sa.select(TollFreeVerification)
            .where(TollFreeVerification.id == verification.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError("Toll-free verification not found")
    return locked


async def _lock_number(
    session: AsyncSession, verification: TollFreeVerification
) -> OrgNumber:
    """Re-read the number the verification points at, under a row lock, so the pair cannot
    be re-pointed or released between validation and the POST."""
    number_id = verification.number_id
    if number_id is None:
        raise NotFoundError("Toll-free verification has no number to file")
    locked = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.id == number_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None or locked.id != verification.number_id:
        raise NotFoundError("Number not found for this toll-free verification")
    return locked


async def _resolve_telnyx_settings(session: AsyncSession, settings: Settings) -> object:
    """The org's ACTIVE Telnyx account laid over the base settings when there is one, else
    the base settings (a platform-operator run against a globally configured key). The
    caller still has to supply a usable key either way."""
    account = await provider_accounts.active_account_for(session, "telnyx")
    if account is None:
        return settings
    return provider_accounts.settings_like_for(settings, account)


async def file_tollfree_verification_with_telnyx(
    session: AsyncSession,
    settings: Settings,
    verification: TollFreeVerification,
    *,
    fields: Mapping[str, Any],
    sole_proprietor: bool,
    client: httpx.AsyncClient | None = None,
) -> TollFreeVerification:
    """Submit ``verification`` to Telnyx exactly once and record the carrier request id.

    ``fields`` carries the operator-supplied request values under their documented
    camelCase names (see ``build_tollfree_verification_payload``); ``sole_proprietor`` is
    required so a business filing cannot silently omit the registration fields. ``client``
    is an injected httpx client (e.g. a ``MockTransport`` in tests); when omitted the
    transport owns and closes its own client, and no live call is ever made unless an
    operator invokes this for real.
    """
    verification = await _lock_verification(session, verification)
    tfv_id = str(verification.id)

    # Fail closed on a terminal local status BEFORE we touch the carrier or write a marker
    # - see _require_fileable_status. This operation only files records that are not yet
    # decided, so a decided record is refused before any carrier activity.
    _require_fileable_status(verification)

    # Lock and re-validate the number the verification belongs to: same org, an actual
    # toll-free number, a Telnyx number, still active.
    number = await _lock_number(session, verification)
    _require_usable_number(verification, number)

    # Repeat guard: any prior attempt marker OR recorded request id refuses a re-file,
    # whatever the (fileable) local status is. This is what makes the legacy `submitted`
    # state safe to accept - it is only accepted with no ref and no marker.
    if (
        _attempt_marker(verification) is not None
        or _carrier_request_id(verification) is not None
    ):
        log.warning("telnyx_tollfree_filing_refused_repeat", tfv_id=tfv_id)
        raise ConflictError(
            "A Telnyx toll-free filing was already attempted for this verification; "
            "automatic retries are refused until the attempted filing is reconciled "
            "externally and its state repaired deliberately"
        )

    # Build and validate the whole carrier body before writing anything durable: the
    # builder fails closed on missing/blank/malformed values, and a rejection days later
    # costs real time. This runs before the marker so a bad payload never blocks a retry.
    payload = build_tollfree_verification_payload(
        phone_number=number.e164,
        fields=fields,
        sole_proprietor=sole_proprietor,
    )

    resolved = await _resolve_telnyx_settings(session, settings)
    api_key = _secret(getattr(resolved, "telnyx_api_key", None)).strip()
    if not api_key:
        raise ValidationFailedError(
            "No Telnyx API key is available; add and verify an active Telnyx account (or "
            "configure one) before filing a toll-free verification"
        )

    # Persist the pending marker and COMMIT it before the POST. This is what stops a
    # timeout/retry (or a second click) from submitting the same request twice: the next
    # caller sees the marker and is refused.
    attempt_id = str(uuid.uuid4())
    refs = _carrier_refs(verification)
    refs[_ATTEMPT_KEY] = {
        "status": "pending",
        "attempt_id": attempt_id,
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }
    verification.carrier_refs = refs
    await session.commit()
    # The commit expires the instance; reload so later attribute reads never trigger a
    # lazy load (illegal under async) and reflect the now-durable marker.
    await session.refresh(verification)

    registration = TelnyxTollfreeVerificationClient(api_key=api_key, client=client)
    try:
        result = await registration.create(payload)
    except Exception as exc:
        # Leave the existing local status and the durable marker untouched: the outcome
        # may be unknown (a timeout can arrive after Telnyx accepted the POST) and there
        # may be no request id to look up. The marker records "attempted, outcome
        # unknown"; retries stay refused until the case is reconciled externally and the
        # state repaired deliberately. Never log the payload, the key, the number or the
        # carrier body.
        log.warning(
            "telnyx_tollfree_filing_failed",
            tfv_id=tfv_id,
            attempt_id=attempt_id,
            error=type(exc).__name__,
        )
        raise
    finally:
        # No-op when the caller injected the client (we do not own it).
        await registration.aclose()

    request_id = result.get("id")
    if not isinstance(request_id, str) or not request_id.strip():
        # A 2xx without an identifier is not a usable success; the marker stays pending.
        raise FeatureUnavailableError(
            "Telnyx returned no request identifier in its response"
        )

    filed = _carrier_refs(verification)
    filed[_REQUEST_ID_KEY] = request_id.strip()
    filed.pop(_ATTEMPT_KEY, None)
    verification.carrier_refs = filed
    # Status is earned, and never regresses: advance_status is monotonic, so a draft
    # verification moves to submitted and one already in the legacy `submitted` state
    # stays there - the carrier ref is what now records the filing. We never set approved
    # here; a locally-submitted verification is not sendable until the carrier itself
    # approves, after which the two still never compare equal locally.
    advance_status(verification, "submitted")
    await session.commit()

    log.info("telnyx_tollfree_filed", tfv_id=tfv_id, attempt_id=attempt_id)
    return verification


#: Convenience alias - the toll-free regime is referred to as "TFV" throughout the code.
file_tfv_with_telnyx = file_tollfree_verification_with_telnyx
