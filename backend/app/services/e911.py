"""E911: registered locations for numbers, emergency dialing, and the 911 notification.

Interconnected VoIP must deliver 911 calls with the caller's registered location (47 CFR
9.11), let a user dial 911 directly with no prefix and notify a central point when they do
(Kari's Law, 9.16), and tell the customer how 911 over VoIP differs from a landline.
Telnyx does the routing; this module registers the address with Telnyx and turns emergency
calling on per number (Telnyx bills $1.50/month per enabled number).

A number's E911 state lives in ``OrgNumber.provisioning["e911"]``:
``{"address_id", "status", "detail", "updated_at"}`` where status is Telnyx's
``emergency_status`` (provisioning / active / deprovisioning / disabled) or our own
``failed`` when the carrier call did not go through - the sweeper retries those.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, NotFoundError, ValidationFailedError
from app.models import EmergencyAddress, OrgNumber

log = structlog.get_logger("e911")

TELNYX = "https://api.telnyx.com/v2"
#: Short codes that must always connect. 933 is the carrier's test line: it reads back the
#: registered address and never reaches a dispatcher.
EMERGENCY_NUMBERS = ("911", "933")
#: Numbers whose E911 is still being set up or needs another attempt.
RETRY_STATES = ("failed", "provisioning", "pending")

#: 47 CFR 9.11(a)(5): what the customer must be told, and acknowledge, before service.
LIMITATIONS_NOTICE = (
    "911 calls from Ringlite are sent with the address you register for each number. If you "
    "use a number somewhere else, update its address first or emergency services may be "
    "sent to the wrong place. 911 will not work during a power, internet or service outage, "
    "or if your account is suspended. Tell everyone who uses these numbers about these "
    "limits."
)


def is_emergency(dialed: str) -> bool:
    return (dialed or "").strip().replace(" ", "") in EMERGENCY_NUMBERS


def status_of(number: OrgNumber) -> dict:
    e911 = (number.provisioning or {}).get("e911") or {}
    return {
        "status": e911.get("status")
        or ("unsupported" if number.carrier != "telnyx" else "missing"),
        "address_id": e911.get("address_id"),
        "detail": e911.get("detail"),
    }


def _set(number: OrgNumber, **fields) -> None:
    provisioning = dict(number.provisioning or {})
    provisioning["e911"] = {
        **(provisioning.get("e911") or {}),
        **fields,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    number.provisioning = provisioning


async def _api_key(session, settings) -> str:
    from app.services import provider_accounts

    account = await provider_accounts.active_account_for(session, "telnyx")
    resolved = (
        settings if account is None else provider_accounts.settings_like_for(settings, account)
    )
    key = resolved.telnyx_api_key.get_secret_value().strip()
    if not key:
        raise FeatureUnavailableError("Emergency addresses cannot be registered right now.")
    return key


async def _telnyx(method: str, path: str, key: str, *, client=None, **kwargs) -> dict:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.request(
            method, f"{TELNYX}{path}", headers={"Authorization": f"Bearer {key}"}, **kwargs
        )
    except httpx.HTTPError as exc:
        raise FeatureUnavailableError("The emergency-calling provider is unreachable.") from exc
    finally:
        if owned:
            await client.aclose()
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        errors = body.get("errors") or [{}]
        detail = errors[0].get("detail") or errors[0].get("title") or str(resp.status_code)
        pointer = ((errors[0].get("source") or {}).get("pointer") or "").strip("/")
        raise ValidationFailedError(
            f"Emergency address was refused: {pointer + ' ' if pointer else ''}{detail}".strip()
        )
    return body.get("data") or {}


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------
def public_address(address: EmergencyAddress) -> dict:
    return {
        "id": str(address.id),
        "name": address.name,
        "street_address": address.street_address,
        "extended_address": address.extended_address,
        "locality": address.locality,
        "administrative_area": address.administrative_area,
        "postal_code": address.postal_code,
        "country_code": address.country_code,
        "label": one_line(address),
    }


def one_line(address: EmergencyAddress) -> str:
    unit = f" {address.extended_address}" if address.extended_address else ""
    return (
        f"{address.street_address}{unit}, {address.locality}, "
        f"{address.administrative_area} {address.postal_code}"
    )


async def create_address(session, settings, org_id: uuid.UUID, fields: dict, *, client=None):
    """Validate the address with the carrier, then register it. A location the carrier
    cannot validate is refused here - never discovered during an emergency."""
    name = (fields.get("name") or "").strip()
    street = (fields.get("street_address") or "").strip()
    unit = (fields.get("extended_address") or "").strip() or None
    city = (fields.get("locality") or "").strip()
    state = (fields.get("administrative_area") or "").strip().upper()
    postal = (fields.get("postal_code") or "").strip()
    country = (fields.get("country_code") or "US").strip().upper()
    if not (name and street and city and state and postal):
        raise ValidationFailedError(
            "Enter the name, street, city, state and ZIP code of where the phone is used."
        )
    if country not in ("US", "CA"):
        raise ValidationFailedError("Emergency addresses must be in the US or Canada.")
    key = await _api_key(session, settings)
    location = {
        "street_address": street,
        "extended_address": unit or "",
        "locality": city,
        "administrative_area": state,
        "postal_code": postal,
        "country_code": country,
    }
    check = await _telnyx("POST", "/addresses/actions/validate", key, client=client, json=location)
    if check.get("result") != "valid":
        suggested = check.get("suggested") or {}
        hint = ", ".join(
            v
            for v in (
                suggested.get("street_address"),
                suggested.get("locality"),
                suggested.get("administrative_area"),
                suggested.get("postal_code"),
            )
            if v
        )
        raise ValidationFailedError(
            "That address could not be verified for emergency services."
            + (f" Did you mean: {hint}?" if hint else " Check the street and ZIP code.")
        )
    first, _, last = name.partition(" ")
    created = await _telnyx(
        "POST",
        "/addresses",
        key,
        client=client,
        json={
            **location,
            "business_name": name,
            "first_name": first or name,
            "last_name": last or first or name,
            "address_book": False,
            "validate_address": True,
        },
    )
    address = EmergencyAddress(
        id=uuid.uuid4(),
        org_id=org_id,
        telnyx_address_id=str(created["id"]),
        name=name,
        street_address=street,
        extended_address=unit,
        locality=city,
        administrative_area=state,
        postal_code=postal,
        country_code=country,
    )
    session.add(address)
    await session.flush()
    log.info("e911_address_registered", org_id=str(org_id), address_id=str(address.id))
    return address


# --------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------
async def enable(session, settings, number: OrgNumber, address: EmergencyAddress, *, client=None):
    """Turn emergency calling on for one number at this address. Never raises for a carrier
    problem: the number is marked ``failed`` and the sweeper retries it."""
    if number.carrier != "telnyx":
        raise ValidationFailedError(
            "Emergency addresses for this number are managed with its original carrier."
        )
    if address.org_id != number.org_id:
        raise NotFoundError("Address not found")
    _set(number, address_id=str(address.id), status="pending", detail=None)
    try:
        key = await _api_key(session, settings)
        found = await _telnyx(
            "GET",
            "/phone_numbers",
            key,
            client=client,
            params={"filter[phone_number]": number.e164},
        )
        rows = found if isinstance(found, list) else []
        if not rows:
            raise ValidationFailedError("The carrier does not list this number on the account.")
        result = await _telnyx(
            "POST",
            f"/phone_numbers/{rows[0]['id']}/actions/enable_emergency",
            key,
            client=client,
            json={"emergency_enabled": True, "emergency_address_id": address.telnyx_address_id},
        )
        status = (
            (result.get("emergency") or {}).get("emergency_status")
            or result.get("emergency_status")
            or "provisioning"
        )
        _set(number, status=status, telnyx_phone_id=str(rows[0]["id"]), detail=None)
    except (FeatureUnavailableError, ValidationFailedError) as exc:
        _set(number, status="failed", detail=exc.message)
        log.warning("e911_enable_failed", number_id=str(number.id), error=exc.message)
    await session.commit()
    return status_of(number)


async def refresh(session, settings, number: OrgNumber, *, client=None) -> None:
    """Read the carrier's emergency_status for a number still provisioning."""
    e911 = (number.provisioning or {}).get("e911") or {}
    phone_id = e911.get("telnyx_phone_id")
    if not phone_id:
        return
    key = await _api_key(session, settings)
    voice = await _telnyx("GET", f"/phone_numbers/{phone_id}/voice", key, client=client)
    status = (voice.get("emergency") or {}).get("emergency_status")
    if status:
        _set(number, status=status)
        await session.commit()


async def enable_for_purchase(session, settings, purchase, *, client=None) -> None:
    """After a purchase: register every newly provisioned number at the buyer's address."""
    if not purchase.emergency_address_id:
        return
    address = await session.get(EmergencyAddress, purchase.emergency_address_id)
    if address is None:
        return
    ids = [uuid.UUID(n["number_id"]) for n in purchase.numbers if n.get("number_id")]
    for number_id in ids:
        number = await session.get(OrgNumber, number_id)
        if number is not None and number.carrier == "telnyx" and number.status != "released":
            await enable(session, settings, number, address, client=client)


async def tick(session_factory, settings, *, client=None) -> dict[str, int]:
    """Retry failed activations and follow provisioning ones until active."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(OrgNumber.id, OrgNumber.org_id, OrgNumber.provisioning)
                .where(OrgNumber.carrier == "telnyx", OrgNumber.status == "active")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).all()
    todo = [
        (nid, org_id)
        for nid, org_id, prov in rows
        if ((prov or {}).get("e911") or {}).get("status") in RETRY_STATES
    ]
    counts = {"checked": len(todo), "active": 0}
    for number_id, org_id in todo:
        async with session_factory() as session:
            set_org_context(session, org_id)
            number = await session.get(OrgNumber, number_id)
            e911 = (number.provisioning or {}).get("e911") or {}
            try:
                if e911.get("status") == "failed" or not e911.get("telnyx_phone_id"):
                    address = await session.get(EmergencyAddress, uuid.UUID(e911["address_id"]))
                    if address is not None:
                        await enable(session, settings, number, address, client=client)
                else:
                    await refresh(session, settings, number, client=client)
                if status_of(number)["status"] == "active":
                    counts["active"] += 1
            except Exception:
                log.exception("e911_tick_failed", number_id=str(number_id))
    return counts


# --------------------------------------------------------------------------------------
# Dialing 911
# --------------------------------------------------------------------------------------
async def pick_caller_id(session, org_id: uuid.UUID, preferred: str | None) -> str | None:
    """The number a 911 call goes out on: the caller's own number when its E911 is active,
    else any of the workspace's numbers with active E911, else the preferred number (the
    carrier still routes it, to a national call centre that asks for the location)."""
    numbers = (
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.org_id == org_id, OrgNumber.status == "active")
                .order_by(OrgNumber.created_at)
            )
        )
        .scalars()
        .all()
    )
    active = [n.e164 for n in numbers if status_of(n)["status"] == "active"]
    if preferred and preferred in active:
        return preferred
    if active:
        return active[0]
    return preferred or (numbers[0].e164 if numbers else None)


async def notify(session, settings, org_id: uuid.UUID, *, caller: str, from_e164: str, dialed: str):
    """Kari's Law notification: tell the workspace's owners and admins that someone
    dialed 911, from which number, and the address it was registered at. Never raises."""
    if dialed.strip() != "911":
        return
    try:
        from app.models import OrgMembership, Role, User
        from app.services import mailer

        emails = (
            (
                await session.execute(
                    sa.select(User.email)
                    .join(OrgMembership, OrgMembership.user_id == User.id)
                    .join(Role, Role.id == OrgMembership.role_id)
                    .where(OrgMembership.org_id == org_id, Role.name.in_(("owner", "admin")))
                )
            )
            .scalars()
            .all()
        )
        number = (
            await session.execute(
                sa.select(OrgNumber).where(OrgNumber.org_id == org_id, OrgNumber.e164 == from_e164)
            )
        ).scalar_one_or_none()
        where = "no registered emergency address"
        if number is not None and status_of(number)["address_id"]:
            address = await session.get(
                EmergencyAddress, uuid.UUID(status_of(number)["address_id"])
            )
            if address is not None:
                where = one_line(address)
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await mailer.send(
            settings,
            sorted(set(emails)),
            "911 was dialed from your Ringlite workspace",
            f"{caller} dialed 911 at {when} from {from_e164}.\n\n"
            f"Registered emergency address: {where}.\n\n"
            "This notice is sent every time 911 is dialed so someone on site can help "
            "responders find the caller.",
        )
    except Exception:
        log.exception("e911_notify_failed", org_id=str(org_id))
