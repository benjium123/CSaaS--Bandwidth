"""P44f: bring numbers in (port-in) and watch numbers leaving (port-out).

PORT-IN is also a fraud vector: someone ports a VICTIM's number onto our platform to
receive their calls and 2FA texts. So a port-in request must pass, before any carrier
sees it:
  - the workspace is business-verified (KYC status that allows telephony);
  - the authorised person or business on the LOA matches the verified business or one of
    its verified people;
  - no number is already active here or in another open request;
  - every number is in the workspace's home region (destination policy);
  - the number is actually portable (the carrier's own portability check);
  - an OPERATOR approves it (status awaiting_review -> submitted).
Telnyx orders are then created, documented and confirmed through the porting API; the
sweeper polls them (``poll_port_ins``) and imports the numbers once ported. SignalWire has
no porting API: an approved request is filed by the operator in the SignalWire dashboard
and its status is set by hand (``set_manual_status``).

P1: the customer can fix a rejected/refused request in place (``update_port_in``) - a
resubmit PATCHes the order the carrier already has instead of filing a new one - and can
stop one that has not reached its date yet (``cancel_port_in``). Every status change tells
the workspace through ``services/porting_notify.py``, which never names our carrier. A
ported number is put straight onto the workspace's approved texting campaign.

PORT-OUT cannot legally be blocked when the request is valid - the carrier port-out PIN is
the control (set on the Telnyx account; see docs/runbooks/PORTING.md). What we do is make
sure nobody is surprised: ``poll_port_outs`` finds carrier port-out requests for our numbers,
alerts the owners and operators at once, and marks the numbers released once gone. The
in-app ``port_locked`` flag stops an account-takeover from releasing a number here.
"""

from __future__ import annotations

import difflib
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import phonenumbers
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import (
    ConflictError,
    FeatureUnavailableError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models import KYC_TELEPHONY_STATUSES, KycPerson, KycProfile, Org, OrgNumber
from app.models.porting import OPEN_PORT_IN_STATUSES, PortRequest

log = structlog.get_logger("porting")

ALLOWED_DOC_TYPES = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}
MAX_DOC_BYTES = 10 * 1024 * 1024
MAX_NUMBERS = 50

#: A customer can still fix the details of these (see update_port_in).
EDITABLE = ("awaiting_review", "rejected", "exception")
#: A customer can still stop these (foc_confirmed has a date and needs support).
CANCELLABLE = ("awaiting_review", "rejected", "exception", "submitted", "in_process")
#: Statuses that are worth telling the customer about (in_process is not).
NOTIFY_STATUSES = ("foc_confirmed", "exception", "ported", "cancelled")
#: Attached to the "ported" notice when a number still has no texting campaign.
CAMPAIGN_HINT = "Pick a texting campaign for them on the Lines page so they can send texts."

_TELNYX_PORT_STATUS = {
    "draft": "exception",  # never confirmed: the filing did not go through
    "submitted": "submitted",
    "in-process": "in_process",
    "exception": "exception",
    "foc-date-confirmed": "foc_confirmed",
    "ported": "ported",
    "cancel-pending": "cancelled",
    "cancelled": "cancelled",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(port: PortRequest, text: str) -> None:
    port.events = [*(port.events or []), {"at": _now().isoformat(), "text": text[:255]}][-50:]


# ------------------------------------------------------------------ customer-facing text

_CARRIER_NAMES = re.compile(r"telnyx|signalwire", re.IGNORECASE)


def _no_carrier(text: str) -> str:
    """A workspace never learns which of OUR carriers we use."""
    return _CARRIER_NAMES.sub("the carrier", text or "")


_CUSTOMER_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("account number",),
        "The account number doesn't match what your current provider has on file. Copy it "
        "exactly from your latest bill.",
    ),
    (
        ("pin", "passcode"),
        "The transfer PIN is wrong or missing. Ask your current provider for the port-out PIN.",
    ),
    (("name",), "The name doesn't match the account holder at your current provider."),
    (("address",), "The service address doesn't match your current provider's records."),
    (
        ("not found", "not on account", "does not belong"),
        "Your current provider says these numbers aren't on that account.",
    ),
    (
        ("invoice", "bill", "loa", "document"),
        "A document was rejected. Upload a clear, recent bill and a signed authorization "
        "letter.",
    ),
)


def customer_reason(raw: str) -> str:
    """Turn the carrier's raw exception text into a sentence a customer can act on."""
    text = str(raw or "").strip()
    lowered = text.lower()
    for keywords, message in _CUSTOMER_REASONS:
        if any(keyword in lowered for keyword in keywords):
            return message
    return f"Your current provider rejected the request: {_no_carrier(text)}"


def _exception_detail(data: dict) -> str:
    """Telnyx answers an exception with a list of {code, description} rows."""
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return ""
    rows = status.get("details")
    if not isinstance(rows, list):
        return ""
    parts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("description") or row.get("code") or "").strip()
        if text:
            parts.append(text)
    return "; ".join(parts)


def _mark_exception(port: PortRequest, raw: str) -> None:
    """Keep the carrier's words for the operator and a readable sentence for the customer."""
    text = str(raw or "").strip()
    if not text:
        return
    port.last_error = text[:255]
    port.details = {**(port.details or {}), "customer_reason": customer_reason(text)}


# ------------------------------------------------------------------ fraud gate helpers


_SUFFIXES = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co|ltd|limited|lp|llp|pllc)\b")


def _norm(name: str) -> str:
    name = _SUFFIXES.sub(" ", (name or "").lower())
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name).split())


def names_match(a: str, b: str) -> bool:
    """Loose match between a name on the LOA and a verified name (case, punctuation, legal
    suffixes and word order ignored)."""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y or sorted(x.split()) == sorted(y.split()):
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.85


def normalize_numbers(raw: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        item = (item or "").strip()
        if not item:
            continue
        try:
            parsed = phonenumbers.parse(item, "US")
        except phonenumbers.NumberParseException as exc:
            raise ValidationFailedError(f"{item} is not a phone number") from exc
        if not phonenumbers.is_valid_number(parsed):
            raise ValidationFailedError(f"{item} is not a valid phone number")
        e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in out:
            out.append(e164)
    if not out:
        raise ValidationFailedError("List at least one number to port")
    if len(out) > MAX_NUMBERS:
        raise ValidationFailedError(f"Port at most {MAX_NUMBERS} numbers per request")
    return out


async def _verified_names(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()
    if profile is None or profile.status not in KYC_TELEPHONY_STATUSES:
        raise PermissionDeniedError(
            "Finish business verification before porting numbers in.",
            code="account_not_verified",
        )
    names = [profile.legal_name or ""]
    people = (
        await session.execute(sa.select(KycPerson.full_name).where(KycPerson.org_id == org_id))
    ).scalars().all()
    return [n for n in [*names, *people] if n]


# ------------------------------------------------------------------ shared form validation


def _names_from_form(form: dict) -> tuple[str, str]:
    authorized = str(form.get("authorized_name") or "").strip()
    business = str(form.get("business_name") or "").strip()
    if not authorized:
        raise ValidationFailedError("Name the person authorising the port (as on the LOA)")
    return authorized, business


def _require_service_fields(form: dict) -> None:
    for key in ("account_number", "service_street", "service_city", "service_state", "service_zip"):
        if not str(form.get(key) or "").strip():
            raise ValidationFailedError(f"{key.replace('_', ' ')} is required")


def _service_address(form: dict) -> dict:
    return {
        "street": str(form.get("service_street")).strip(),
        "extended": str(form.get("service_extended") or "").strip(),
        "city": str(form.get("service_city")).strip(),
        "state": str(form.get("service_state")).strip().upper(),
        "zip": str(form.get("service_zip")).strip(),
    }


def _validate_document(label: str, doc: tuple[bytes, str]) -> tuple[bytes, str]:
    data, ctype = doc
    if ctype not in ALLOWED_DOC_TYPES:
        raise ValidationFailedError(f"The {label.upper()} must be a PDF, PNG or JPEG")
    if not data or len(data) > MAX_DOC_BYTES:
        raise ValidationFailedError(f"The {label.upper()} must be between 1 byte and 10 MB")
    return data, ctype


def _names_are_verified(authorized: str, business: str, verified: list[str]) -> bool:
    return any(names_match(authorized, v) or names_match(business, v) for v in verified)


# ------------------------------------------------------------------ Telnyx API


def _carrier(registry, name: str):  # noqa: ANN001, ANN202
    carrier = registry.get(name) if registry is not None else None
    if carrier is None:
        raise FeatureUnavailableError(f"{name} is not configured")
    return carrier


async def _tx(carrier, method: str, path: str, **kwargs) -> httpx.Response:  # noqa: ANN001
    client = await carrier._get_client()
    try:
        return await client.request(
            method,
            f"{carrier.base_url}{path}",
            headers={"Authorization": f"Bearer {carrier.api_key}"},
            **kwargs,
        )
    except httpx.TransportError as exc:
        raise FeatureUnavailableError(f"Telnyx unreachable: {exc}") from exc


def _tx_error(resp: httpx.Response) -> str:
    try:
        errors = (resp.json() or {}).get("errors") or []
        if errors and isinstance(errors[0], dict):
            return str(errors[0].get("detail") or errors[0].get("title"))[:200]
    except ValueError:
        pass
    return f"HTTP {resp.status_code}"


def _order_status_value(resp: httpx.Response) -> str:
    """The order's status value from a GET /porting_orders/{id} body."""
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        return ""
    status = data.get("status") if isinstance(data, dict) else None
    if isinstance(status, dict):
        status = status.get("value")
    return str(status or "")


async def portability_check(registry, numbers: list[str]) -> list[dict]:  # noqa: ANN001
    """Ask Telnyx whether each number can be ported. Never raises for a refused number."""
    carrier = _carrier(registry, "telnyx")
    resp = await _tx(carrier, "POST", "/portability_checks", json={"phone_numbers": numbers})
    if resp.status_code >= 400:
        raise ValidationFailedError(f"Portability check failed: {_tx_error(resp)}")
    out = []
    for row in (resp.json() or {}).get("data") or []:
        out.append(
            {
                "phone_number": row.get("phone_number"),
                "portable": bool(row.get("portable")),
                "reason": row.get("not_portable_reason"),
                "fast_portable": bool(row.get("fast_portable")),
            }
        )
    return out


# ------------------------------------------------------------------ port-in


async def create_port_in(
    session: AsyncSession,
    settings,  # noqa: ANN001
    store,  # noqa: ANN001
    org_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    carrier: str,
    numbers: list[str],
    form: dict,
    loa: tuple[bytes, str],
    invoice: tuple[bytes, str],
    registry=None,  # noqa: ANN001
) -> PortRequest:
    """Validate and store a port-in request for operator review. Commits."""
    from app.services import credentials, destination_policy, phone_region

    if carrier not in ("telnyx", "signalwire"):
        raise ValidationFailedError("Port to telnyx or signalwire")
    numbers = normalize_numbers(numbers)
    home = await phone_region.for_org(session, org_id)
    for e164 in numbers:
        if destination_policy.decide(e164, home) is not None:
            raise ValidationFailedError(f"{e164} is outside the numbers this account may hold")

    verified = await _verified_names(session, org_id)
    authorized, business = _names_from_form(form)
    if not _names_are_verified(authorized, business, verified):
        raise PermissionDeniedError(
            "The name on the port request must match your verified business or one of its "
            "verified owners.",
            code="port_name_mismatch",
        )
    _require_service_fields(form)

    known = (
        await session.execute(
            sa.select(OrgNumber.e164, OrgNumber.is_active, OrgNumber.org_id)
            .where(OrgNumber.e164.in_(numbers))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    for e164, active, owner in known:
        if active:
            raise ConflictError(f"{e164} is already active on Ringlite")
        if owner != org_id:
            # A number another workspace once held (released) - its row cannot simply be
            # handed over, so support moves it by hand.
            raise ConflictError(
                f"{e164} was used on Ringlite before - contact support to port it in"
            )
    open_rows = (
        await session.execute(
            sa.select(PortRequest.numbers)
            .where(PortRequest.direction == "in", PortRequest.status.in_(OPEN_PORT_IN_STATUSES))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    pending = {n for row in open_rows for n in (row or [])}
    if pending & set(numbers):
        raise ConflictError(f"{sorted(pending & set(numbers))[0]} already has a port request open")

    # P1: ask the losing carrier BEFORE an operator spends time on it. A number Telnyx
    # refuses (or does not answer for) never reaches review at all.
    if carrier == "telnyx" and registry is not None and registry.get("telnyx") is not None:
        results = await portability_check(registry, numbers)
        by_number = {r.get("phone_number"): r for r in results if r.get("phone_number")}
        refused = []
        for e164 in numbers:
            row = by_number.get(e164)
            if row is None:
                refused.append((e164, None))
            elif not row.get("portable"):
                refused.append((e164, row.get("reason")))
        if refused:
            listed = ", ".join(f"{n} ({reason or 'not portable'})" for n, reason in refused)
            raise ValidationFailedError(f"These numbers can't be moved: {listed}")

    docs = {}
    for label, doc in (("loa", loa), ("invoice", invoice)):
        docs[label] = _validate_document(label, doc)

    set_org_context(session, org_id)
    port = PortRequest(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="in",
        carrier=carrier,
        numbers=numbers,
        status="awaiting_review",
        submitted_by=user_id,
        details={
            "authorized_name": authorized,
            "business_name": business,
            "account_number": str(form.get("account_number")).strip(),
            "billing_number": str(form.get("billing_number") or numbers[0]).strip(),
            "service_address": _service_address(form),
        },
    )
    pin = str(form.get("pin") or "").strip()
    if pin:
        port.secret_enc = credentials.encrypt(settings, {"pin": pin})
    for label, (data, ctype) in docs.items():
        key = f"porting/{org_id}/{port.id}/{label}.{ALLOWED_DOC_TYPES[ctype]}"
        await store.put(key, data, ctype)
        setattr(port, f"{label}_media_key", key)
    _event(port, "Submitted for review")
    session.add(port)
    from app.services import card_risk

    await card_risk.open_alert(
        session, org_id, "port_in_review", {"port_id": str(port.id), "numbers": numbers}
    )
    await session.commit()
    return port


async def update_port_in(
    session: AsyncSession,
    settings,  # noqa: ANN001
    store,  # noqa: ANN001
    registry,  # noqa: ANN001 - kept for symmetry with create/approve
    port: PortRequest,
    *,
    form: dict,
    loa: tuple[bytes, str] | None = None,
    invoice: tuple[bytes, str] | None = None,
    user_id: uuid.UUID | None,
) -> PortRequest:
    """P1: the customer fixes a request that was rejected or the carrier refused.

    The numbers themselves are NOT editable (cancel and start a new request for those).
    Documents are replaced only when new ones are given, under fresh keys, so what the
    operator already reviewed is never overwritten in place. Commits.
    """
    from app.services import credentials

    if port.direction != "in" or port.status not in EDITABLE:
        raise ConflictError("This port request can no longer be changed")
    verified = await _verified_names(session, port.org_id)
    authorized, business = _names_from_form(form)
    if not _names_are_verified(authorized, business, verified):
        raise PermissionDeniedError(
            "The name on the port request must match your verified business or one of its "
            "verified owners.",
            code="port_name_mismatch",
        )
    _require_service_fields(form)

    details = dict(port.details or {})
    details["authorized_name"] = authorized
    details["business_name"] = business
    details["account_number"] = str(form.get("account_number")).strip()
    details["billing_number"] = str(
        form.get("billing_number") or (port.numbers or [""])[0]
    ).strip()
    details["service_address"] = _service_address(form)

    pin = str(form.get("pin") or "").strip()
    if pin:
        port.secret_enc = credentials.encrypt(settings, {"pin": pin})

    seq = len(port.events or []) + 1
    for label, doc in (("loa", loa), ("invoice", invoice)):
        if doc is None:
            continue
        data, ctype = _validate_document(label, doc)
        key = f"porting/{port.org_id}/{port.id}/{label}-{seq}.{ALLOWED_DOC_TYPES[ctype]}"
        await store.put(key, data, ctype)
        setattr(port, f"{label}_media_key", key)

    if port.status == "awaiting_review":
        _event(port, "Details updated")
    else:
        # A stale reason would make a successful refiling read as another exception, and a
        # stale customer sentence has nothing to do with the fixed request.
        port.last_error = None
        details.pop("customer_reason", None)
        if port.status == "exception":
            # The order is already with the carrier: approve() PATCHes it instead of
            # filing a second one.
            details["resubmit"] = True
        _event(port, "Resubmitted for review")
        port.status = "awaiting_review"
    if port.submitted_by is None:
        port.submitted_by = user_id
    port.details = details
    await session.commit()
    return port


async def cancel_port_in(
    session: AsyncSession,
    registry,  # noqa: ANN001
    port: PortRequest,
    *,
    user_id: uuid.UUID | None,
    settings=None,  # noqa: ANN001
) -> PortRequest:
    """P1: the customer stops a transfer that has not reached its date. Commits."""
    if port.direction != "in":
        raise ConflictError("Only a port-in request can be cancelled")
    if port.status == "foc_confirmed":
        raise ConflictError("The transfer date is already set; contact support to stop it.")
    if port.status not in CANCELLABLE:
        raise ConflictError("This port request can no longer be cancelled")
    orders = [str(o) for o in (port.details or {}).get("orders") or []]
    if port.carrier == "telnyx" and orders:
        carrier = registry.get("telnyx") if registry is not None else None
        if carrier is None:
            raise ConflictError(
                "We could not reach the carrier to stop this transfer. Please contact support."
            )
        for order_id in orders:
            resp = await _tx(carrier, "POST", f"/porting_orders/{order_id}/actions/cancel")
            if resp.status_code >= 400:
                raise ConflictError(
                    "The carrier would not stop this transfer yet. Please contact support."
                )
    port.status = "cancelled"
    if port.submitted_by is None:
        port.submitted_by = user_id
    _event(port, "Cancelled by the customer")
    await session.commit()
    if settings is not None:
        from app.services import porting_notify

        await porting_notify.notify(session, settings, port, "cancelled")
        await session.commit()
    return port


async def _upload_document(carrier, store, key: str) -> str:  # noqa: ANN001
    data = await store.get(key)
    name = key.rsplit("/", 1)[-1]
    ctype = next((c for c, ext in ALLOWED_DOC_TYPES.items() if name.endswith(ext)), "application/pdf")
    resp = await _tx(carrier, "POST", "/documents", files={"file": (name, data, ctype)})
    if resp.status_code >= 400:
        raise ValidationFailedError(f"Telnyx refused the document: {_tx_error(resp)}")
    return str(((resp.json() or {}).get("data") or {}).get("id") or "")


async def approve(
    session: AsyncSession, settings, store, registry, port: PortRequest, operator_id: uuid.UUID  # noqa: ANN001
) -> PortRequest:
    """Operator approval: file the port with the carrier. Commits."""
    from app.services import credentials, porting_notify

    if port.direction != "in" or port.status != "awaiting_review":
        raise ConflictError("Only a port-in awaiting review can be approved")
    port.reviewed_by = operator_id
    port.reviewed_at = _now()
    # A failed earlier attempt leaves its error behind; a clean refiling must not read it
    # as a new exception (the status below is derived from last_error).
    port.last_error = None
    if port.carrier == "signalwire":
        port.status = "submitted"
        port.details = {**(port.details or {}), "manual": True}
        _event(port, "Approved - file it in the SignalWire dashboard (Phone Numbers > Port Requests)")
        await session.commit()
        if settings is not None:
            await porting_notify.notify(session, settings, port, "submitted")
            await session.commit()
        return port

    carrier = _carrier(registry, "telnyx")
    details = dict(port.details or {})
    pin = ""
    if port.secret_enc:
        pin = str(credentials.decrypt(settings, port.secret_enc).get("pin") or "")
    loa_id = await _upload_document(carrier, store, port.loa_media_key)
    invoice_id = await _upload_document(carrier, store, port.invoice_media_key)
    # P1: a fixed-up request PATCHes the order the carrier already holds - filing a new one
    # would abandon the numbers already in flight.
    resubmit = bool(details.get("resubmit")) and bool(details.get("orders"))
    if resubmit:
        orders = [str(o) for o in details.get("orders") or []]
    else:
        resp = await _tx(carrier, "POST", "/porting_orders", json={"phone_numbers": port.numbers})
        if resp.status_code >= 400:
            _mark_exception(port, _tx_error(resp))
            _event(port, f"Telnyx refused the order: {port.last_error}")
            await session.commit()
            raise ValidationFailedError(f"Telnyx refused the port: {port.last_error}")
        orders = [str(o.get("id")) for o in (resp.json() or {}).get("data") or [] if o.get("id")]
    address = details.get("service_address") or {}
    config = {"tags": ["csaas", f"csaas-org-{port.org_id}"]}
    if getattr(settings, "telnyx_voice_connection_id", ""):
        config["connection_id"] = settings.telnyx_voice_connection_id
    if getattr(settings, "telnyx_messaging_profile_id", ""):
        config["messaging_profile_id"] = settings.telnyx_messaging_profile_id
    body = {
        "customer_reference": str(port.id),
        "end_user": {
            "admin": {
                "entity_name": details.get("business_name") or details.get("authorized_name"),
                "auth_person_name": details.get("authorized_name"),
                "billing_phone_number": details.get("billing_number"),
                "account_number": details.get("account_number"),
                "pin_passcode": pin or None,
            },
            "location": {
                "street_address": address.get("street"),
                "extended_address": address.get("extended") or None,
                "locality": address.get("city"),
                "administrative_area": address.get("state"),
                "postal_code": address.get("zip"),
                "country_code": "US",
            },
        },
        "documents": {"loa": loa_id, "invoice": invoice_id},
        "phone_number_configuration": config,
    }
    for order_id in orders:
        patched = await _tx(carrier, "PATCH", f"/porting_orders/{order_id}", json=body)
        if patched.status_code >= 400:
            _mark_exception(port, _tx_error(patched))
            _event(port, f"Telnyx order {order_id} could not be completed: {port.last_error}")
            continue
        if resubmit:
            current = await _tx(carrier, "GET", f"/porting_orders/{order_id}")
            if current.status_code != 200 or _order_status_value(current) != "draft":
                continue  # already confirmed; confirming again would be refused
        confirmed = await _tx(carrier, "POST", f"/porting_orders/{order_id}/actions/confirm")
        if confirmed.status_code >= 400:
            _mark_exception(port, _tx_error(confirmed))
            _event(port, f"Telnyx order {order_id} could not be submitted: {port.last_error}")
    port.carrier_ref = orders[0] if orders else None
    updated = {k: v for k, v in (port.details or {}).items() if k != "resubmit"}
    updated["orders"] = orders
    port.details = updated
    port.status = "submitted" if orders and not port.last_error else "exception"
    _event(port, f"Filed with Telnyx ({len(orders)} order(s))")
    await session.commit()
    if settings is not None:
        if port.status == "exception":
            await porting_notify.notify(
                session,
                settings,
                port,
                "exception",
                reason=(port.details or {}).get("customer_reason"),
            )
        else:
            await porting_notify.notify(session, settings, port, "submitted")
        await session.commit()
    return port


async def reject(
    session: AsyncSession,
    port: PortRequest,
    operator_id: uuid.UUID,
    reason: str,
    *,
    settings=None,  # noqa: ANN001
) -> PortRequest:
    if port.status != "awaiting_review":
        raise ConflictError("Only a port-in awaiting review can be rejected")
    port.status = "rejected"
    port.reviewed_by = operator_id
    port.reviewed_at = _now()
    port.last_error = reason[:255]
    _event(port, f"Rejected: {reason}")
    await session.commit()
    if settings is not None:
        from app.services import porting_notify

        await porting_notify.notify(session, settings, port, "rejected", reason=reason)
        await session.commit()
    return port


async def _import(session: AsyncSession, registry, port: PortRequest) -> int:  # noqa: ANN001
    """Create org_numbers rows for a completed port-in (idempotent)."""
    set_org_context(session, port.org_id)
    carrier = registry.get(port.carrier) if registry is not None else None
    added = 0
    for e164 in port.numbers or []:
        existing = (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.e164 == e164)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if existing is not None and existing.is_active and existing.org_id == port.org_id:
            continue  # already imported
        if existing is not None and existing.org_id != port.org_id:
            # Never report a port "done" while the number sits on someone else's record.
            port.last_error = f"{e164} is recorded on another workspace - move it by hand"
            from app.services import card_risk

            await card_risk.open_alert(
                session,
                port.org_id,
                "port_in_import_blocked",
                {"port_id": str(port.id), "number": e164},
            )
            continue
        ref = None
        if carrier is not None and port.carrier == "telnyx":
            resp = await _tx(carrier, "GET", "/phone_numbers", params={"filter[phone_number]": e164})
            if resp.status_code == 200:
                rows = (resp.json() or {}).get("data") or []
                ref = str(rows[0].get("id")) if rows else None
        if existing is not None:
            # This workspace's own released number coming back: reactivate the row.
            set_org_context(session, port.org_id)
            existing.is_active = True
            existing.status = "active"
            existing.released_at = None
            existing.carrier = port.carrier
            existing.provider_ref = ref
            existing.purchased_at = _now()
            added += 1
            continue
        parsed = phonenumbers.parse(e164, None)
        kind = (
            "tollfree"
            if phonenumbers.number_type(parsed) == phonenumbers.PhoneNumberType.TOLL_FREE
            else "local"
        )
        set_org_context(session, port.org_id)
        session.add(
            OrgNumber(
                id=uuid.uuid4(),
                org_id=port.org_id,
                e164=e164,
                carrier=port.carrier,
                number_type=kind,
                status="active",
                is_active=True,
                provider_ref=ref,
                capabilities={"sms": True, "mms": True, "voice": True},
                purchased_at=_now(),
            )
        )
        added += 1
    return added


async def _campaign_hint(session: AsyncSession, settings, port: PortRequest) -> str | None:  # noqa: ANN001
    """P1: put just-imported numbers on the workspace's approved texting campaign, and tell
    the customer when one of them still needs a campaign picked."""
    if settings is None or not port.numbers:
        return None
    from app.services import tendlc

    set_org_context(session, port.org_id)
    await tendlc.associate_new_numbers(session, settings, port.org_id)
    set_org_context(session, port.org_id)
    waiting = (
        await session.execute(
            sa.select(sa.func.count(OrgNumber.id)).where(
                OrgNumber.e164.in_(port.numbers),
                OrgNumber.number_type == "local",
                OrgNumber.is_active.is_(True),
                OrgNumber.campaign_id.is_(None),
            )
        )
    ).scalar_one()
    return CAMPAIGN_HINT if waiting else None


async def set_manual_status(
    session: AsyncSession, registry, port: PortRequest, status: str, *, foc_date: str | None, note: str,  # noqa: ANN001
    settings=None,  # noqa: ANN001
) -> PortRequest:
    """Operator-driven status for ports filed by hand (SignalWire)."""
    if status not in ("in_process", "exception", "foc_confirmed", "ported", "cancelled"):
        raise ValidationFailedError("Unknown port status")
    # Only an APPROVED, still-open, hand-filed port-in moves by hand: never one waiting
    # for review (that would import numbers nobody approved), a finished one or a port-out.
    if (
        port.direction != "in"
        or not (port.details or {}).get("manual")
        or port.status in ("awaiting_review", "ported", "rejected", "cancelled")
    ):
        raise ConflictError("This port request cannot be updated by hand")
    port.status = status
    if foc_date:
        port.foc_date = foc_date[:32]
    _event(port, f"{status}: {note}" if note else status)
    if status == "ported":
        await _import(session, registry, port)
    await session.commit()
    if settings is not None and status in NOTIFY_STATUSES:
        reason = None
        if status == "ported":
            reason = await _campaign_hint(session, settings, port)
        elif status == "exception":
            reason = (port.details or {}).get("customer_reason")
        from app.services import porting_notify

        await porting_notify.notify(session, settings, port, status, reason=reason)
        await session.commit()
    return port


async def poll_port_ins(session: AsyncSession, settings, registry) -> int:  # noqa: ANN001
    """Sweeper: follow Telnyx porting orders; import numbers once ported. Commits per row."""
    carrier = registry.get("telnyx") if registry is not None else None
    if carrier is None:
        return 0
    rows = (
        await session.execute(
            sa.select(PortRequest.id, PortRequest.org_id)
            .where(
                PortRequest.direction == "in",
                PortRequest.carrier == "telnyx",
                PortRequest.carrier_ref.is_not(None),
                PortRequest.status.in_(("submitted", "in_process", "exception", "foc_confirmed")),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    changed = 0
    for port_id, org_id in rows:
        set_org_context(session, org_id)
        port = await session.get(PortRequest, port_id)
        if port is None:
            continue
        statuses = []
        for order_id in (port.details or {}).get("orders") or [port.carrier_ref]:
            resp = await _tx(carrier, "GET", f"/porting_orders/{order_id}")
            if resp.status_code != 200:
                continue
            data = (resp.json() or {}).get("data") or {}
            raw = (data.get("status") or {}).get("value") if isinstance(data.get("status"), dict) else data.get("status")
            mapped = _TELNYX_PORT_STATUS.get(str(raw), port.status)
            statuses.append(mapped)
            if mapped == "exception":
                # P1: the carrier says WHAT it wants fixed; keep its words for the operator
                # and a sentence the customer can act on.
                detail = _exception_detail(data)
                if detail:
                    _mark_exception(port, detail)
            foc = data.get("activation_settings", {}).get("foc_datetime_actual") if isinstance(data.get("activation_settings"), dict) else None
            if foc:
                port.foc_date = str(foc)[:32]
        if not statuses:
            continue
        # The request is only "ported" once every order is.
        new = "ported" if all(s == "ported" for s in statuses) else next(
            (s for s in ("exception", "in_process", "foc_confirmed", "submitted") if s in statuses),
            statuses[0],
        )
        previous = port.status
        if new != port.status:
            port.status = new
            _event(port, f"Carrier status: {new}")
            changed += 1
            if new == "ported":
                await _import(session, registry, port)
        await session.commit()
        if new != previous and settings is not None and new in NOTIFY_STATUSES:
            reason = None
            if new == "ported":
                reason = await _campaign_hint(session, settings, port)
            elif new == "exception":
                reason = (port.details or {}).get("customer_reason")
            from app.services import porting_notify

            await porting_notify.notify(session, settings, port, new, reason=reason)
            await session.commit()
    return changed


# ------------------------------------------------------------------ port-out watch


async def poll_port_outs(session: AsyncSession, settings, registry) -> int:  # noqa: ANN001
    """Sweeper: find carrier port-out requests for our numbers, alert, and release numbers
    that have left. Commits per port-out."""
    from app.services import billing_alerts, card_risk

    carrier = registry.get("telnyx") if registry is not None else None
    if carrier is None:
        return 0
    resp = await _tx(carrier, "GET", "/portouts", params={"page[size]": 100})
    if resp.status_code != 200:
        return 0
    handled = 0
    for row in (resp.json() or {}).get("data") or []:
        portout_id = str(row.get("id") or "")
        numbers = [str(n) for n in (row.get("phone_numbers") or []) if n]
        status = str(row.get("status") or "pending").lower()
        if not portout_id or not numbers:
            continue
        ours = (
            await session.execute(
                sa.select(OrgNumber)
                .where(
                    OrgNumber.e164.in_(numbers),
                    OrgNumber.carrier == "telnyx",
                    OrgNumber.is_active.is_(True),
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
        if not ours:
            continue
        org_id = ours[0].org_id
        set_org_context(session, org_id)
        existing = (
            await session.execute(
                sa.select(PortRequest).where(
                    PortRequest.direction == "out", PortRequest.carrier_ref == portout_id
                )
            )
        ).scalar_one_or_none()
        mapped = PORT_OUT_STATUS_MAP.get(status, "pending")
        if existing is None:
            existing = PortRequest(
                id=uuid.uuid4(),
                org_id=org_id,
                direction="out",
                carrier="telnyx",
                numbers=[n.e164 for n in ours],
                status=mapped,
                carrier_ref=portout_id,
                foc_date=str(row.get("foc_date") or "")[:32] or None,
                details={
                    "gaining_carrier": str(row.get("carrier_name") or "")[:80] or None,
                    "requested_foc_date": str(row.get("requested_foc_date") or "")[:32] or None,
                    "respond_by": (_now() + PORT_OUT_RESPOND_WITHIN).isoformat(),
                },
            )
            _event(existing, f"Carrier reported a port-out request ({status})")
            session.add(existing)
            org = await session.get(Org, org_id)
            nums = ", ".join(n.e164 for n in ours)
            await card_risk.open_alert(
                session, org_id, "port_out_request", {"portout": portout_id, "numbers": nums}
            )
            if org is not None:
                await billing_alerts.notify_owners(
                    session,
                    settings,
                    org,
                    f"Port-out requested for {nums}",
                    f"Another carrier has asked to take {nums} away from your account. "
                    f"{settings.app_name} reviews every such request before the numbers are "
                    "released. If you did not request this, open Numbers > Porting and press "
                    "\"I didn't request this\", or contact support IMMEDIATELY - someone may be "
                    "trying to hijack your number.",
                    dedupe_key=f"portout:{portout_id}",
                )
            to_operators = (
                f"Port-out to decide: {nums}",
                f"{org.name if org is not None else org_id}: another carrier "
                f"({row.get('carrier_name') or 'unknown'}) asked for {nums}.\n"
                "Approve or reject it in Ops > Ports within 24 hours. Telnyx authorizes it "
                "on its own if nobody answers.",
            )
            handled += 1
        else:
            if existing.status != mapped:
                existing.status = mapped
                _event(existing, f"Carrier status: {status}")
            to_operators = _port_out_reminder(existing) if existing.status == "pending" else None
        if mapped == "ported":
            for number in ours:
                if number.is_active:
                    number.is_active = False
                    number.status = "released"
                    number.released_at = _now()
        await session.commit()
        # Only after the commit: never email about a request that was not recorded.
        if to_operators is not None:
            await _email_operators(settings, *to_operators)
    return handled


# ------------------------------------------------------------------ port-out decisions
#: Telnyx authorizes a port-out it gets no answer to within 24-48 hours; we answer inside 24.
PORT_OUT_RESPOND_WITHIN = timedelta(hours=24)
#: Operators get one reminder when a request is still undecided this long after it arrived.
PORT_OUT_REMIND_AFTER = timedelta(hours=12)
PORT_OUT_STATUS_MAP = {
    "pending": "pending",
    "authorized": "authorized",
    "rejected-pending": "rejected",
    "rejected": "rejected",
    "ported": "ported",
    "completed": "ported",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}
#: Telnyx's catch-all rejection code; it needs a written reason.
REJECT_OTHER = 1001


async def _email_operators(settings, subject: str, body: str) -> None:  # noqa: ANN001
    """Best effort: an operator email must never break the poll."""
    try:
        from app.services import email_delivery, mailer

        to = await email_delivery._operator_emails()
        if to:
            await mailer.send(settings, to, subject, body)
    except Exception:  # noqa: BLE001
        log.exception("port_out_operator_email_failed")


def _port_out_reminder(port: PortRequest) -> tuple[str, str] | None:
    """One reminder to operators when a port-out is still undecided after 12 hours: marks
    the request and returns the (subject, body) to send once the change is committed."""
    details = dict(port.details or {})
    if details.get("reminded") or port.created_at is None:
        return None
    created = port.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if _now() - created < PORT_OUT_REMIND_AFTER:
        return None
    details["reminded"] = True
    port.details = details
    _event(port, "Reminder sent to operators: still waiting for a decision")
    return (
        f"REMINDER port-out still undecided: {', '.join(port.numbers or [])}",
        "Telnyx will authorize it on its own soon. Decide in Ops > Ports.",
    )


def _port_out_open(port: PortRequest) -> None:
    if port.direction != "out":
        raise ConflictError("This is not a request to move numbers away")
    if port.status != "pending":
        raise ConflictError("This request has already been decided")
    if not port.carrier_ref:
        raise ConflictError("This request has no carrier order")


async def authorize_port_out(
    session: AsyncSession,
    registry,  # noqa: ANN001
    port: PortRequest,
    user_id,  # noqa: ANN001
    *,
    note: str = "",
) -> PortRequest:
    """Operator approval: tell Telnyx to release the numbers."""
    _port_out_open(port)
    carrier = _carrier(registry, "telnyx")
    note = (note or "").strip()[:255]
    body = {"reason": note} if note else {}
    resp = await _tx(carrier, "PATCH", f"/portouts/{port.carrier_ref}/authorized", json=body)
    if resp.status_code >= 300:
        raise ConflictError(f"The carrier refused the approval: {_tx_error(resp)}")
    port.status = "authorized"
    port.reviewed_by = user_id
    port.reviewed_at = _now()
    _event(port, "Approved by Ringlite" + (f": {note}" if note else ""))
    await session.commit()
    return port


async def port_out_rejection_codes(registry, port: PortRequest) -> list[dict]:  # noqa: ANN001
    """The reasons Telnyx accepts for rejecting THIS order. Always offers "Other" (which
    needs a written reason) so an operator is never stuck if the list cannot be read."""
    _port_out_open(port)
    carrier = _carrier(registry, "telnyx")
    try:
        resp = await _tx(carrier, "GET", f"/portouts/rejections/{port.carrier_ref}")
        rows = (resp.json() or {}).get("data") if resp.status_code == 200 else None
    except (FeatureUnavailableError, ValueError):
        rows = None
    codes: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("code") is None:
            continue
        try:
            code = int(row["code"])
        except (TypeError, ValueError):
            continue
        codes.append(
            {
                "code": code,
                "label": str(row.get("reason") or row.get("description") or code)[:120],
                "reason_required": bool(row.get("reason_required")) or code == REJECT_OTHER,
            }
        )
    if not any(c["code"] == REJECT_OTHER for c in codes):
        codes.append({"code": REJECT_OTHER, "label": "Other", "reason_required": True})
    return codes


async def reject_port_out(
    session: AsyncSession,
    registry,  # noqa: ANN001
    port: PortRequest,
    user_id,  # noqa: ANN001
    *,
    code: int,
    reason: str = "",
) -> PortRequest:
    """Operator rejection with one of Telnyx's codes. A valid, authorized port cannot be
    blocked for business reasons; Telnyx's porting team reviews every rejection."""
    _port_out_open(port)
    reason = (reason or "").strip()
    if code == REJECT_OTHER and len(reason) < 5:
        raise ValidationFailedError("Write the reason for rejecting this request")
    carrier = _carrier(registry, "telnyx")
    body: dict = {"rejection_code": code}
    if reason:
        body["reason"] = reason[:255]
    resp = await _tx(
        carrier, "PATCH", f"/portouts/{port.carrier_ref}/rejected-pending", json=body
    )
    if resp.status_code >= 300:
        raise ConflictError(f"The carrier refused the rejection: {_tx_error(resp)}")
    port.status = "rejected"
    port.reviewed_by = user_id
    port.reviewed_at = _now()
    port.last_error = (reason or f"code {code}")[:255]
    _event(port, f"Rejected by Ringlite (code {code})" + (f": {reason}" if reason else ""))
    await session.commit()
    return port


async def dispute_port_out(
    session: AsyncSession, settings, port: PortRequest, user_id  # noqa: ANN001
) -> PortRequest:
    """The workspace says it did not ask for this transfer: flag it to operators at once."""
    if port.direction != "out":
        raise ConflictError("This is not a request to move numbers away")
    if port.status != "pending":
        raise ConflictError("This request has already been decided; contact support")
    details = dict(port.details or {})
    if details.get("disputed_by"):
        return port
    from app.services import card_risk

    details["disputed_by"] = str(user_id) if user_id else "api"
    details["disputed_at"] = _now().isoformat()
    port.details = details
    _event(port, "The workspace says it did not request this transfer")
    await card_risk.open_alert(
        session,
        port.org_id,
        "port_out_disputed",
        {"port": str(port.id), "numbers": ", ".join(port.numbers or [])},
    )
    await session.commit()
    await _email_operators(
        settings,
        f"URGENT port-out DISPUTED by the owner: {', '.join(port.numbers or [])}",
        "The workspace says it did not request this transfer. Reject it in Ops > Ports "
        "(PIN/authorisation mismatch) and treat it as an account-takeover attempt.",
    )
    return port
