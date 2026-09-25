"""P44e: a validated 911 address on every calling number.

Flow:
  1. The workspace registers a service address (``create_address``). It is checked by the
     carrier (Telnyx address validation; SignalWire validates on create) and stored with each
     carrier's own address id in ``carrier_refs``. A refused address comes back with the
     carrier's suggested corrections, shown to the user.
  2. The address is bound to a number (``assign``): Telnyx ``enable_emergency``, SignalWire
     ``e911_address``. The number goes to ``pending`` and the sweeper (``refresh_pending``)
     polls until the carrier reports ``active`` (or failed).
  3. A workspace's FIRST valid address is attached automatically to every number that has
     none (``auto_assign``), so a new number bought later is covered without a click.
  4. Outbound calls (``require_e911``): a number whose E911 is ``none`` or ``failed`` may not
     place calls once its grace period is over. ``pending`` may call - the carrier is still
     provisioning. Calls TO 911 are never refused, whatever the state.

Grace: numbers that existed before enforcement began get E911_GRACE_DAYS from
``E911_ENFORCEMENT_START``; numbers created after that must have an address from day one.

Only Telnyx and SignalWire numbers are supported (the only carriers with live numbers).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, PermissionDeniedError, ValidationFailedError
from app.models import EmergencyAddress, OrgNumber

log = structlog.get_logger("e911")

SUPPORTED_CARRIERS = ("telnyx", "signalwire")
EMERGENCY_NUMBERS = frozenset({"911", "933", "+1911", "+1933"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------- carriers


class CarrierAddressRefused(ValidationFailedError):
    """The carrier refused the address. ``suggestions`` are its corrected candidates."""

    def __init__(self, message: str, suggestions: list[dict] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []


def _split_street(line1: str) -> tuple[str, str]:
    """SignalWire wants the house number and the street name separately."""
    parts = line1.strip().split(" ", 1)
    if len(parts) == 2 and any(ch.isdigit() for ch in parts[0]):
        return parts[0], parts[1]
    return "", line1.strip()


def _names(caller_name: str) -> tuple[str, str]:
    parts = caller_name.strip().split(" ", 1)
    return (parts[0], parts[1] if len(parts) > 1 else parts[0])


async def _telnyx_request(carrier, method: str, path: str, json: dict | None = None) -> httpx.Response:  # noqa: ANN001
    client = await carrier._get_client()
    try:
        return await client.request(
            method,
            f"{carrier.base_url}{path}",
            json=json,
            headers={"Authorization": f"Bearer {carrier.api_key}"},
        )
    except httpx.TransportError as exc:
        raise FeatureUnavailableError(f"Telnyx unreachable: {exc}") from exc


def _relay_base(carrier) -> str:  # noqa: ANN001
    return carrier.base_url.split("/api/laml", 1)[0] + "/api/relay/rest"


async def _signalwire_request(carrier, method: str, path: str, json: dict | None = None) -> httpx.Response:  # noqa: ANN001
    client = await carrier._get_client()
    try:
        return await client.request(
            method,
            f"{_relay_base(carrier)}{path}",
            json=json,
            auth=(carrier.project_id, carrier._api_token),
        )
    except httpx.TransportError as exc:
        raise FeatureUnavailableError(f"SignalWire unreachable: {exc}") from exc


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("detail") or first.get("title") or first)[:200]
        return str(first)[:200]
    return str(body)[:200]


async def _telnyx_create_address(carrier, address: EmergencyAddress) -> str:  # noqa: ANN001
    location = {
        "street_address": address.line1,
        "extended_address": address.line2 or "",
        "locality": address.city,
        "administrative_area": address.state,
        "postal_code": address.postal_code,
        "country_code": address.country,
    }
    check = await _telnyx_request(carrier, "POST", "/addresses/actions/validate", location)
    body = {}
    try:
        body = (check.json() or {}).get("data") or {}
    except ValueError:
        pass
    if check.status_code >= 400 or body.get("result") == "invalid":
        suggestions = body.get("suggested") or body.get("suggested_corrections") or []
        if isinstance(suggestions, dict):
            suggestions = [suggestions]
        raise CarrierAddressRefused(
            "That address could not be verified for 911. Check it, or pick one of the "
            "suggestions.",
            suggestions,
        )
    first, last = _names(address.caller_name)
    resp = await _telnyx_request(
        carrier,
        "POST",
        "/addresses",
        {
            **location,
            "first_name": first,
            "last_name": last,
            "business_name": address.caller_name,
            # Shared Telnyx account: mark our objects (the csaas ownership boundary).
            "customer_reference": "csaas",
        },
    )
    if resp.status_code >= 400:
        raise CarrierAddressRefused(f"Telnyx refused the address: {_error_text(resp)}")
    return str(((resp.json() or {}).get("data") or {}).get("id") or "")


async def _signalwire_create_address(carrier, address: EmergencyAddress) -> str:  # noqa: ANN001
    number, street = _split_street(address.line1)
    first, last = _names(address.caller_name)
    payload = {
        "label": (address.label or address.caller_name)[:64],
        "country": address.country,
        "first_name": first,
        "last_name": last,
        "street_number": number,
        "street_name": street,
        "city": address.city,
        "state": address.state,
        "postal_code": address.postal_code,
        "emergency_enabled": True,
    }
    if address.line2:
        payload["address_type"] = "Suite"
        payload["address_number"] = address.line2
    resp = await _signalwire_request(carrier, "POST", "/addresses", payload)
    if resp.status_code == 422:
        candidates = []
        try:
            candidates = (resp.json() or {}).get("candidates") or []
        except ValueError:
            pass
        raise CarrierAddressRefused(
            "That address could not be verified for 911. Check it, or pick one of the "
            "suggestions.",
            candidates,
        )
    if resp.status_code >= 400:
        raise CarrierAddressRefused(f"SignalWire refused the address: {_error_text(resp)}")
    return str((resp.json() or {}).get("id") or "")


async def _carrier_address_id(
    carrier_name: str, carrier, address: EmergencyAddress  # noqa: ANN001
) -> str:
    """The carrier's id for this address, creating it there on first use."""
    refs = dict(address.carrier_refs or {})
    if refs.get(carrier_name):
        return refs[carrier_name]
    if carrier_name == "telnyx":
        ref = await _telnyx_create_address(carrier, address)
    elif carrier_name == "signalwire":
        ref = await _signalwire_create_address(carrier, address)
    else:
        raise FeatureUnavailableError(f"911 addresses are not supported on {carrier_name} yet")
    refs[carrier_name] = ref
    address.carrier_refs = refs
    address.status = "valid"
    address.last_error = None
    return ref


_TELNYX_STATUS = {
    "active": "active",
    "provisioning": "pending",
    "deprovisioning": "pending",
    "provisioning-failed": "failed",
    "disabled": "none",
}


async def _telnyx_number_id(carrier, number: OrgNumber) -> str:  # noqa: ANN001
    """Telnyx's phone-number id for ``number``. ``provider_ref`` holds the ORDER id on
    Telnyx (see providers/telnyx/numbers.py), so resolve it by e164 like release_number."""
    client = await carrier._get_client()
    try:
        resp = await client.get(
            f"{carrier.base_url}/phone_numbers",
            params={"filter[phone_number]": number.e164},
            headers={"Authorization": f"Bearer {carrier.api_key}"},
        )
    except httpx.TransportError as exc:
        raise FeatureUnavailableError(f"Telnyx unreachable: {exc}") from exc
    rows = (resp.json() or {}).get("data") or [] if resp.status_code == 200 else []
    if not rows or not rows[0].get("id"):
        raise FeatureUnavailableError(f"Telnyx has no number {number.e164} on this account")
    return str(rows[0]["id"])


async def _enable_on_number(carrier_name: str, carrier, number: OrgNumber, ref: str) -> str:  # noqa: ANN001
    """Bind the carrier address to the number. Returns our e911 status."""
    if carrier_name == "telnyx":
        number_id = await _telnyx_number_id(carrier, number)
        resp = await _telnyx_request(
            carrier,
            "POST",
            f"/phone_numbers/{number_id}/actions/enable_emergency",
            {"emergency_enabled": True, "emergency_address_id": ref},
        )
        if resp.status_code >= 400:
            raise ValidationFailedError(f"Telnyx could not enable 911: {_error_text(resp)}")
        data = (resp.json() or {}).get("data") or {}
        status = ((data.get("emergency") or {}).get("emergency_status")) or "provisioning"
        return _TELNYX_STATUS.get(str(status), "pending")
    if not number.provider_ref:
        raise FeatureUnavailableError("This number has no carrier id yet - try again shortly")
    resp = await _signalwire_request(
        carrier,
        "POST",
        f"/phone_numbers/{number.provider_ref}/e911_address",
        {"e911_address_id": ref},
    )
    if resp.status_code >= 400:
        raise ValidationFailedError(f"SignalWire could not enable 911: {_error_text(resp)}")
    return "pending"


async def _carrier_status(carrier_name: str, carrier, number: OrgNumber) -> str | None:  # noqa: ANN001
    if carrier_name == "telnyx":
        try:
            number_id = await _telnyx_number_id(carrier, number)
        except FeatureUnavailableError:
            return None
        resp = await _telnyx_request(carrier, "GET", f"/phone_numbers/{number_id}")
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or {}
        status = (data.get("emergency") or {}).get("emergency_status")
        return _TELNYX_STATUS.get(str(status)) if status else None
    if not number.provider_ref:
        return None
    resp = await _signalwire_request(carrier, "GET", f"/phone_numbers/{number.provider_ref}")
    if resp.status_code != 200:
        return None
    status = str((resp.json() or {}).get("e911_status") or "")
    return {"active": "active", "pending": "pending", "failed": "failed"}.get(status)


# ---------------------------------------------------------------------------- service


def _clean(payload: dict) -> dict:
    out = {k: str(payload.get(k) or "").strip() for k in (
        "label", "caller_name", "line1", "line2", "city", "state", "postal_code", "country"
    )}
    out["country"] = (out["country"] or "US").upper()
    out["state"] = out["state"].upper()
    missing = [k for k in ("caller_name", "line1", "city", "state", "postal_code") if not out[k]]
    if missing:
        raise ValidationFailedError(f"911 address is missing: {', '.join(missing)}")
    if out["country"] != "US":
        raise ValidationFailedError("911 addresses are available for US numbers only for now.")
    if len(out["state"]) != 2:
        raise ValidationFailedError("Use the two-letter state code (for example TX).")
    return out


def _registry_carrier(registry, name: str):  # noqa: ANN001, ANN202
    carrier = registry.get(name) if registry is not None else None
    if carrier is None:
        raise FeatureUnavailableError(f"{name} is not configured")
    return carrier


async def create_address(
    session: AsyncSession,
    registry,  # noqa: ANN001 - CarrierRegistry
    org_id: uuid.UUID,
    payload: dict,
    *,
    user_id: uuid.UUID | None,
) -> EmergencyAddress:
    """Create and carrier-validate an address, then attach it to numbers that have none.

    Validated against every supported carrier the workspace has numbers on (the address
    must exist at the carrier that will route the 911 call). Does not commit."""
    fields = _clean(payload)
    set_org_context(session, org_id)
    address = EmergencyAddress(id=uuid.uuid4(), org_id=org_id, created_by=user_id, **fields)
    address.line2 = fields["line2"] or None
    session.add(address)
    carriers = (
        await session.execute(
            sa.select(OrgNumber.carrier)
            .where(
                OrgNumber.org_id == org_id,
                OrgNumber.is_active.is_(True),
                OrgNumber.carrier.in_(SUPPORTED_CARRIERS),
            )
            .distinct()
        )
    ).scalars().all()
    for name in carriers or ["telnyx"]:
        carrier = registry.get(name) if registry is not None else None
        if carrier is None:
            continue
        await _carrier_address_id(name, carrier, address)
    await session.flush()
    await auto_assign(session, registry, org_id)
    return address


async def assign(
    session: AsyncSession, registry, number: OrgNumber, address: EmergencyAddress  # noqa: ANN001
) -> OrgNumber:
    """Bind ``address`` to ``number`` at the number's carrier. Does not commit."""
    if number.org_id != address.org_id:
        raise PermissionDeniedError("That address belongs to another workspace")
    if number.carrier not in SUPPORTED_CARRIERS:
        raise FeatureUnavailableError(
            f"911 addresses are not supported on {number.carrier} numbers yet"
        )
    carrier = _registry_carrier(registry, number.carrier)
    try:
        ref = await _carrier_address_id(number.carrier, carrier, address)
        status = await _enable_on_number(number.carrier, carrier, number, ref)
        number.e911_error = None
    except CarrierAddressRefused as exc:
        address.status = "invalid"
        address.last_error = str(exc)[:255]
        raise
    except (ValidationFailedError, FeatureUnavailableError) as exc:
        status = "failed"
        number.e911_error = str(exc)[:255]
    number.emergency_address_id = address.id
    number.e911_status = status
    number.e911_updated_at = _now()
    return number


async def auto_assign(session: AsyncSession, registry, org_id: uuid.UUID) -> int:  # noqa: ANN001
    """Attach the workspace's first valid address to every active number without one.
    Never raises; returns how many numbers were bound."""
    set_org_context(session, org_id)
    address = (
        await session.execute(
            sa.select(EmergencyAddress)
            .where(EmergencyAddress.org_id == org_id, EmergencyAddress.status == "valid")
            .order_by(EmergencyAddress.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if address is None:
        return 0
    numbers = (
        await session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.org_id == org_id,
                OrgNumber.is_active.is_(True),
                OrgNumber.emergency_address_id.is_(None),
                OrgNumber.carrier.in_(SUPPORTED_CARRIERS),
            )
        )
    ).scalars().all()
    done = 0
    for number in numbers:
        try:
            await assign(session, registry, number, address)
            done += 1
        except Exception:  # noqa: BLE001 - one bad number must not stop the rest
            log.exception("e911.auto_assign_failed", number=number.e164)
    return done


async def auto_assign_all(session: AsyncSession, registry) -> int:  # noqa: ANN001
    """Sweeper: cover numbers bought (or ported in) since the workspace saved its address.
    Commits per workspace."""
    org_ids = (
        await session.execute(
            sa.select(OrgNumber.org_id)
            .where(
                OrgNumber.is_active.is_(True),
                OrgNumber.emergency_address_id.is_(None),
                OrgNumber.carrier.in_(SUPPORTED_CARRIERS),
                OrgNumber.org_id.in_(
                    sa.select(EmergencyAddress.org_id).where(EmergencyAddress.status == "valid")
                ),
            )
            .distinct()
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    done = 0
    for org_id in org_ids:
        try:
            done += await auto_assign(session, registry, org_id)
            await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception("e911.auto_assign_all_failed", org_id=str(org_id))
    return done


async def refresh_pending(session: AsyncSession, registry) -> int:  # noqa: ANN001
    """Sweeper: poll the carrier for numbers still provisioning. Commits per number."""
    rows = (
        await session.execute(
            sa.select(OrgNumber.id, OrgNumber.org_id)
            .where(OrgNumber.e911_status == "pending", OrgNumber.is_active.is_(True))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    changed = 0
    for number_id, org_id in rows:
        set_org_context(session, org_id)
        number = await session.get(OrgNumber, number_id)
        if number is None:
            continue
        carrier = registry.get(number.carrier) if registry is not None else None
        if carrier is None:
            continue
        try:
            status = await _carrier_status(number.carrier, carrier, number)
        except Exception:  # noqa: BLE001
            log.exception("e911.refresh_failed", number=number.e164)
            continue
        if status and status != number.e911_status:
            number.e911_status = status
            number.e911_updated_at = _now()
            changed += 1
        await session.commit()
    return changed


def grace_deadline(settings, number: OrgNumber) -> datetime:  # noqa: ANN001
    start_raw = str(getattr(settings, "e911_enforcement_start", "") or "2026-09-26")
    try:
        start = datetime.fromisoformat(start_raw).replace(tzinfo=timezone.utc)
    except ValueError:
        start = datetime(2026, 9, 26, tzinfo=timezone.utc)
    grace = start + timedelta(days=int(getattr(settings, "e911_grace_days", 7)))
    created = _aware(number.purchased_at) or _aware(getattr(number, "created_at", None))
    if created is None:
        return grace
    return max(created + timedelta(days=int(getattr(settings, "e911_grace_days", 7))), grace)


async def require_e911(
    session: AsyncSession,
    settings,  # noqa: ANN001
    org_id: uuid.UUID,
    from_e164: str,
    to: str,
) -> None:
    """Refuse an outbound call from a number without working 911 (after its grace)."""
    if not getattr(settings, "e911_enforced", True):
        return
    if "".join(ch for ch in to if ch.isdigit() or ch == "+") in EMERGENCY_NUMBERS:
        return
    number = (
        await session.execute(
            sa.select(OrgNumber).where(OrgNumber.org_id == org_id, OrgNumber.e164 == from_e164)
        )
    ).scalar_one_or_none()
    if number is None or number.carrier not in SUPPORTED_CARRIERS:
        return
    if number.e911_status in ("active", "pending"):
        return
    if _now() < grace_deadline(settings, number):
        return
    raise PermissionDeniedError(
        f"Add a 911 address to {from_e164} before calling from it (Numbers -> Emergency "
        "address). US law requires a registered location for emergency calls.",
        code="e911_required",
    )
