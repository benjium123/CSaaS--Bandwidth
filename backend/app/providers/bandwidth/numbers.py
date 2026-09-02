"""Bandwidth number provisioning.

Bandwidth's Numbers API is XML and runs on the dashboard API host, separate from the
messaging endpoint. It uses the same Basic credentials and account id as messaging, but
orders additionally require a Bandwidth SiteId. Keep the provisioning half as a mixin for
the same reason as Telnyx/Twilio/Plivo: one carrier adapter should own one credential, not
split it across two objects.

The XML API is async for orders: a successful order is RECEIVED, and completion is
observable only by polling `order_status(provider_ref)`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult

DEFAULT_NUMBERS_BASE_URL = "https://dashboard.bandwidth.com/api"

# Payload guard: no DTD parsing, and a hard ceiling on the XML size we are willing to
# consider. This is stdlib ElementTree plus an explicit bound - no new dependency.
_MAX_XML_BYTES = 1_000_000


def _local(tag: str) -> str:
    """Return the tag name without any XML namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def _xml_escape(value: str) -> str:
    """Escape text for an XML body. `e164` never contains `&`, but SiteId/Name may."""
    return escape(str(value))


def _find_text(element: ET.Element, tag: str) -> str:
    """Find the first descendant with the given local tag and return its text."""
    for child in element.iter():
        if _local(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def _parse_xml(text: str) -> ET.Element:
    """Parse a Bandwidth XML response with small-hostile-input guards.

    Bandwidth can return arbitrary carrier XML. We never need DTDs, so reject them
    explicitly and keep a hard 1MB cap before ElementTree is ever called.
    """
    if len(text.encode("utf-8")) > _MAX_XML_BYTES:
        raise FeatureUnavailableError("Bandwidth XML response exceeded the 1MB safety limit")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValidationFailedError("Bandwidth returned XML with a DTD, which is not accepted")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeatureUnavailableError(f"Bandwidth returned malformed XML: {exc}") from exc


def _raise_if_auth_rejected(resp: httpx.Response) -> None:
    """Bandwidth's Numbers/Dashboard API is Basic-auth only (api_username/api_password) -
    NEVER the OAuth2 client credentials the messaging API accepts. A 401/403 here almost
    always means the wrong credential pair was passed, not a genuinely bad request; say
    so plainly rather than surfacing a generic status-code message."""
    if resp.status_code in (401, 403):
        raise ValidationFailedError(
            "Bandwidth Numbers API rejected the API user credentials (it needs "
            "api_username/api_password, not OAuth client credentials)"
        )


def _national10(e164: str) -> str:
    """Convert a US e164 to the 10-digit national form Bandwidth order APIs expect."""
    digits = "".join(ch for ch in e164 if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def _available_from_raw(
    raw: str, query: NumberSearch, region: str = "", locality: str = ""
) -> AvailableNumber | None:
    """Build an AvailableNumber from a Bandwidth FullNumber/TelephoneNumber value.

    None when `raw` does not carry a real 10-digit US number - an empty/garbage entry
    must be dropped, never turned into a synthetic "+1" placeholder result.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits[-10:]
    if len(national) != 10:
        return None
    return AvailableNumber(
        e164=f"+1{national}",
        number_type=query.number_type,
        region=region,
        locality=locality,
        monthly_cost="",
        setup_cost="",
        capabilities={"sms": True, "mms": True, "voice": True},
        monthly_cost_cents=None,
        setup_cost_cents=None,
    )


@dataclass(frozen=True)
class OrderStatusResult:
    """A dedicated order-status return; Bandwidth can describe a failure here.

    The sweeper accepts either an OrderResult or this shape, so this file does not need to
    pretend a terminal failure is an order.
    """

    status: str
    detail: str | None = None


class BandwidthNumberProviderMixin:
    """Provisioning half of the Bandwidth adapter.

    Requires the composing class to provide `_get_client()`, `_auth`, `account_id`, and
    `site_id` (the last is introduced by the Phase 18 adapter integration notes).
    """

    numbers_base_url = DEFAULT_NUMBERS_BASE_URL

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        params: dict[str, object] = {
            "quantity": min(max(query.limit, 1), 100),
            "enableTNDetail": "true",
        }
        if query.area_code:
            params["areaCode"] = query.area_code
        if query.number_type == "tollfree":
            params["tollFree"] = "true"

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.numbers_base_url}/accounts/{self.account_id}/availableNumbers",
                params=params,
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Bandwidth unreachable: {exc}") from exc

        _raise_if_auth_rejected(resp)
        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"Bandwidth number search failed with {resp.status_code}"
            )

        root = _parse_xml(resp.text)
        out: list[AvailableNumber] = []

        # Shape 1: enriched detail records.
        for el in root.iter():
            if _local(el.tag) != "TelephoneNumberDetail":
                continue
            raw = _find_text(el, "FullNumber")
            if not raw:
                continue
            number = _available_from_raw(
                raw,
                query,
                region=_find_text(el, "State"),
                locality=_find_text(el, "City"),
            )
            if number is not None:
                out.append(number)

        # Shape 2: plain telephone list.
        for el in root.iter():
            if _local(el.tag) != "TelephoneNumber":
                continue
            raw = (el.text or "").strip()
            if not raw:
                continue
            number = _available_from_raw(raw, query)
            if number is not None:
                out.append(number)

        # Deduplicate by e164 because a response could theoretically contain both shapes.
        seen: set[str] = set()
        unique: list[AvailableNumber] = []
        for number in out:
            if number.e164 not in seen:
                seen.add(number.e164)
                unique.append(number)
        return unique

    async def order_number(self, e164: str) -> OrderResult:
        site_id = getattr(self, "site_id", "")
        if not site_id:
            raise ValidationFailedError("bandwidth_site_id is required to order numbers")

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Order>"
            f"<Name>csaas {_xml_escape(e164)}</Name>"
            f"<SiteId>{_xml_escape(site_id)}</SiteId>"
            "<ExistingTelephoneNumberOrderType><TelephoneNumberList>"
            f"<TelephoneNumber>{_xml_escape(_national10(e164))}</TelephoneNumber>"
            "</TelephoneNumberList></ExistingTelephoneNumberOrderType>"
            "</Order>"
        )

        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.numbers_base_url}/accounts/{self.account_id}/orders",
                content=body,
                headers={"Content-Type": "application/xml"},
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Bandwidth unreachable: {exc}") from exc

        _raise_if_auth_rejected(resp)
        if resp.status_code not in (200, 201, 202):
            raise ValidationFailedError(
                f"Bandwidth refused the order: {resp.text[:255] or resp.status_code}"
            )

        root = _parse_xml(resp.text)
        order_id = _find_text(root, "id")
        raw_status = _find_text(root, "OrderStatus").upper()
        # A 2xx response can still carry an ErrorList (partial/rejected order) or omit
        # the order id entirely - either means there is nothing to poll later, so this
        # must fail loudly now rather than persist a "pending" row that can never
        # resolve.
        has_errors = any(_local(el.tag) == "ErrorList" for el in root.iter())
        if not order_id or has_errors:
            description = _find_text(root, "Description")
            raise ValidationFailedError(
                (description or f"Bandwidth rejected the order for {e164}")[:255]
            )

        return OrderResult(
            e164=e164,
            provider_ref=order_id,
            # Bandwidth's RECEIVED means accepted; only COMPLETE means routable.
            status="active" if raw_status == "COMPLETE" else "pending",
            capabilities={"sms": True, "mms": True, "voice": True},
            monthly_cost_cents=None,
            setup_cost_cents=None,
        )

    async def order_status(self, provider_ref: str) -> OrderStatusResult:
        client = await self._get_client()
        resp = await client.get(
            f"{self.numbers_base_url}/accounts/{self.account_id}/orders/"
            f"{quote(provider_ref, safe='')}",
            auth=self._auth,
        )
        _raise_if_auth_rejected(resp)
        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"Bandwidth order status failed with {resp.status_code}"
            )

        root = _parse_xml(resp.text)
        raw = _find_text(root, "OrderStatus").upper()
        description = _find_text(root, "Description")

        if raw == "COMPLETE":
            return OrderStatusResult(status="active")
        if raw == "FAILED":
            return OrderStatusResult(
                status="failed",
                detail=description or f"Bandwidth order {provider_ref} failed",
            )
        if raw in ("RECEIVED", "BACKORDERED", "PARTIAL"):
            return OrderStatusResult(status="pending", detail=description or raw)
        return OrderStatusResult(status="pending", detail=description or raw or "pending")

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<DisconnectTelephoneNumberOrder>"
            f"<name>csaas release {_xml_escape(e164)}</name>"
            "<DisconnectTelephoneNumberOrderType><TelephoneNumberList>"
            f"<TelephoneNumber>{_xml_escape(_national10(e164))}</TelephoneNumber>"
            "</TelephoneNumberList></DisconnectTelephoneNumberOrderType>"
            "</DisconnectTelephoneNumberOrder>"
        )

        client = await self._get_client()
        resp = await client.post(
            f"{self.numbers_base_url}/accounts/{self.account_id}/disconnects",
            content=body,
            headers={"Content-Type": "application/xml"},
            auth=self._auth,
        )
        _raise_if_auth_rejected(resp)
        if resp.status_code in (200, 201, 202, 204, 404, 409):
            # 404/409 mean the carrier already has no such number - release is
            # idempotent by definition and must not fail repeated sweeps.
            return
        raise ValidationFailedError(
            f"Bandwidth refused to release {e164}: {resp.status_code}"
        )
