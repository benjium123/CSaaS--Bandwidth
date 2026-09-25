"""P44f: bring numbers in (port-in) and watch numbers leaving (port-out).

PORT-IN is also a fraud vector: someone ports a VICTIM's number onto our platform to
receive their calls and 2FA texts. So a port-in request must pass, before any carrier
sees it:
  - the workspace is business-verified (KYC status that allows telephony);
  - the authorised person or business on the LOA matches the verified business or one of
    its verified people;
  - no number is already active here or in another open request;
  - every number is in the workspace's home region (destination policy);
  - an OPERATOR approves it (status awaiting_review -> submitted).
Telnyx orders are then created, documented and confirmed through the porting API; the
sweeper polls them (``poll_port_ins``) and imports the numbers once ported. SignalWire has
no porting API: an approved request is filed by the operator in the SignalWire dashboard
and its status is set by hand (``set_manual_status``).

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
from datetime import datetime, timezone

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

_TELNYX_PORT_STATUS = {
    "draft": "submitted",
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
    authorized = str(form.get("authorized_name") or "").strip()
    business = str(form.get("business_name") or "").strip()
    if not authorized:
        raise ValidationFailedError("Name the person authorising the port (as on the LOA)")
    if not any(names_match(authorized, v) or names_match(business, v) for v in verified):
        raise PermissionDeniedError(
            "The name on the port request must match your verified business or one of its "
            "verified owners.",
            code="port_name_mismatch",
        )
    for key in ("account_number", "service_street", "service_city", "service_state", "service_zip"):
        if not str(form.get(key) or "").strip():
            raise ValidationFailedError(f"{key.replace('_', ' ')} is required")

    taken = (
        await session.execute(
            sa.select(OrgNumber.e164)
            .where(OrgNumber.e164.in_(numbers), OrgNumber.is_active.is_(True))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    if taken:
        raise ConflictError(f"{taken[0]} is already active on Ringlite")
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

    docs = {}
    for label, (data, ctype) in (("loa", loa), ("invoice", invoice)):
        if ctype not in ALLOWED_DOC_TYPES:
            raise ValidationFailedError(f"The {label.upper()} must be a PDF, PNG or JPEG")
        if not data or len(data) > MAX_DOC_BYTES:
            raise ValidationFailedError(f"The {label.upper()} must be between 1 byte and 10 MB")
        docs[label] = (data, ctype)

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
            "service_address": {
                "street": str(form.get("service_street")).strip(),
                "extended": str(form.get("service_extended") or "").strip(),
                "city": str(form.get("service_city")).strip(),
                "state": str(form.get("service_state")).strip().upper(),
                "zip": str(form.get("service_zip")).strip(),
            },
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
    from app.services import credentials

    if port.direction != "in" or port.status != "awaiting_review":
        raise ConflictError("Only a port-in awaiting review can be approved")
    port.reviewed_by = operator_id
    port.reviewed_at = _now()
    if port.carrier == "signalwire":
        port.status = "submitted"
        port.details = {**(port.details or {}), "manual": True}
        _event(port, "Approved - file it in the SignalWire dashboard (Phone Numbers > Port Requests)")
        await session.commit()
        return port

    carrier = _carrier(registry, "telnyx")
    details = port.details or {}
    pin = ""
    if port.secret_enc:
        pin = str(credentials.decrypt(settings, port.secret_enc).get("pin") or "")
    loa_id = await _upload_document(carrier, store, port.loa_media_key)
    invoice_id = await _upload_document(carrier, store, port.invoice_media_key)
    resp = await _tx(carrier, "POST", "/porting_orders", json={"phone_numbers": port.numbers})
    if resp.status_code >= 400:
        port.last_error = _tx_error(resp)
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
            port.last_error = _tx_error(patched)
            _event(port, f"Telnyx order {order_id} could not be completed: {port.last_error}")
            continue
        confirmed = await _tx(carrier, "POST", f"/porting_orders/{order_id}/actions/confirm")
        if confirmed.status_code >= 400:
            port.last_error = _tx_error(confirmed)
            _event(port, f"Telnyx order {order_id} could not be submitted: {port.last_error}")
    port.carrier_ref = orders[0] if orders else None
    port.details = {**details, "orders": orders}
    port.status = "submitted" if orders and not port.last_error else "exception"
    _event(port, f"Filed with Telnyx ({len(orders)} order(s))")
    await session.commit()
    return port


async def reject(session: AsyncSession, port: PortRequest, operator_id: uuid.UUID, reason: str) -> PortRequest:
    if port.status != "awaiting_review":
        raise ConflictError("Only a port-in awaiting review can be rejected")
    port.status = "rejected"
    port.reviewed_by = operator_id
    port.reviewed_at = _now()
    port.last_error = reason[:255]
    _event(port, f"Rejected: {reason}")
    await session.commit()
    return port


async def _import(session: AsyncSession, registry, port: PortRequest) -> int:  # noqa: ANN001
    """Create org_numbers rows for a completed port-in (idempotent)."""
    set_org_context(session, port.org_id)
    carrier = registry.get(port.carrier) if registry is not None else None
    added = 0
    for e164 in port.numbers or []:
        exists = (
            await session.execute(
                sa.select(OrgNumber.id)
                .where(OrgNumber.e164 == e164)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).first()
        if exists is not None:
            continue
        ref = None
        if carrier is not None and port.carrier == "telnyx":
            resp = await _tx(carrier, "GET", "/phone_numbers", params={"filter[phone_number]": e164})
            if resp.status_code == 200:
                rows = (resp.json() or {}).get("data") or []
                ref = str(rows[0].get("id")) if rows else None
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


async def set_manual_status(
    session: AsyncSession, registry, port: PortRequest, status: str, *, foc_date: str | None, note: str  # noqa: ANN001
) -> PortRequest:
    """Operator-driven status for ports filed by hand (SignalWire)."""
    if status not in ("in_process", "exception", "foc_confirmed", "ported", "cancelled"):
        raise ValidationFailedError("Unknown port status")
    port.status = status
    if foc_date:
        port.foc_date = foc_date[:32]
    _event(port, f"{status}: {note}" if note else status)
    if status == "ported":
        await _import(session, registry, port)
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
            statuses.append(_TELNYX_PORT_STATUS.get(str(raw), port.status))
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
        if new != port.status:
            port.status = new
            _event(port, f"Carrier status: {new}")
            changed += 1
            if new == "ported":
                await _import(session, registry, port)
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
                .where(OrgNumber.e164.in_(numbers), OrgNumber.carrier == "telnyx")
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
        mapped = {"ported": "ported", "completed": "ported", "rejected": "rejected",
                  "canceled": "cancelled", "cancelled": "cancelled"}.get(status, "pending")
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
                    f"Another carrier has asked to take {nums} away from your account. If you "
                    "did not request this, contact support IMMEDIATELY - someone may be trying "
                    "to hijack your number.",
                    dedupe_key=f"portout:{portout_id}",
                )
            handled += 1
        elif existing.status != mapped:
            existing.status = mapped
            _event(existing, f"Carrier status: {status}")
        if mapped == "ported":
            for number in ours:
                if number.is_active:
                    number.is_active = False
                    number.status = "released"
                    number.released_at = _now()
        await session.commit()
    return handled
