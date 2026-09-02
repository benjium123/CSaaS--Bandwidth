"""Telnyx number provisioning.

Telnyx's ordering API is JSON and synchronous enough to model honestly: search returns
availability, an order returns a status we report as-is rather than assuming success.

Mixed into the messaging adapter rather than living beside it, because on Telnyx the two
share one API key and one base URL - splitting them would mean two objects holding the same
credential for no gain.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult, parse_cost_cents


def _capabilities(features: object) -> dict:
    """Telnyx reports features as a list of {"name": "sms"} objects."""
    names: set[str] = set()
    if isinstance(features, list):
        for item in features:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]).lower())
            elif isinstance(item, str):
                names.add(item.lower())
    return {"sms": "sms" in names, "mms": "mms" in names, "voice": "voice" in names}


@dataclass(frozen=True)
class OrderStatusResult:
    """P18: Telnyx's async order-status return, same shape as Bandwidth's - the sweeper
    (services/number_orders.py) accepts either."""

    status: str
    detail: str | None = None


class TelnyxNumberProviderMixin:
    """Provisioning half of the Telnyx adapter. Requires `_get_client`, `api_key`, `base_url`."""

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        params: dict[str, object] = {
            "filter[limit]": min(max(query.limit, 1), 100),
            "filter[country_code]": "US",
        }
        if query.number_type == "tollfree":
            params["filter[number_type]"] = "toll_free"
        else:
            params["filter[number_type]"] = "local"
            if query.area_code:
                params["filter[national_destination_code]"] = query.area_code
        if query.contains:
            params["filter[phone_number][contains]"] = query.contains
        if query.locality:
            params["filter[locality]"] = query.locality
        if query.region:
            params["filter[administrative_area]"] = query.region

        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/available_phone_numbers",
            params=params,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"Telnyx number search failed with {resp.status_code}"
            )
        payload = resp.json()
        out: list[AvailableNumber] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            region = item.get("region_information") or {}
            cost_info = item.get("cost_information") or {}
            out.append(
                AvailableNumber(
                    e164=str(item.get("phone_number") or ""),
                    number_type=(
                        "tollfree" if item.get("phone_number_type") == "toll_free" else "local"
                    ),
                    region=str(region.get("administrative_area") or "")
                    if isinstance(region, dict)
                    else "",
                    locality=str(region.get("locality") or "") if isinstance(region, dict) else "",
                    monthly_cost=str(cost_info.get("monthly_cost", "")),
                    setup_cost=str(cost_info.get("upfront_cost", "")),
                    capabilities=_capabilities(item.get("features")),
                    monthly_cost_cents=parse_cost_cents(cost_info.get("monthly_cost")),
                    setup_cost_cents=parse_cost_cents(cost_info.get("upfront_cost")),
                )
            )
        return [n for n in out if n.e164]

    async def order_number(self, e164: str) -> OrderResult:
        client = await self._get_client()
        body: dict = {"phone_numbers": [{"phone_number": e164}]}
        if getattr(self, "messaging_profile_id", ""):
            body["messaging_profile_id"] = self.messaging_profile_id

        try:
            resp = await client.post(
                f"{self.base_url}/number_orders",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Telnyx unreachable: {exc}") from exc

        if resp.status_code not in (200, 201, 202):
            detail = ""
            try:
                errors = resp.json().get("errors") or []
                if errors:
                    detail = str(errors[0].get("detail") or errors[0].get("title") or "")
            except ValueError:
                detail = resp.text[:200]
            raise ValidationFailedError(f"Telnyx refused the order: {detail or resp.status_code}")

        data = (resp.json() or {}).get("data") or {}
        entries = data.get("phone_numbers") or []
        entry = entries[0] if entries and isinstance(entries[0], dict) else {}
        order_status = str(data.get("status") or "").lower()
        return OrderResult(
            e164=str(entry.get("phone_number") or e164),
            provider_ref=str(entry.get("id") or data.get("id") or ""),
            # Report what the carrier SAID. Assuming "active" on a pending order means
            # inbound is silently dropped until it really provisions.
            status="active" if order_status == "success" else "pending",
            capabilities=_capabilities(entry.get("features")),
        )

    async def order_status(self, provider_ref: str) -> OrderStatusResult:
        """P18: Telnyx number orders are not always synchronous - a `number_orders`
        create can come back "pending" and only later settle to "success" or "failed".
        Polled by services/number_orders.py exactly like Bandwidth's order_status."""
        client = await self._get_client()
        resp = await client.get(
            f"{self.base_url}/number_orders/{quote(provider_ref, safe='')}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"Telnyx order status failed with {resp.status_code}"
            )

        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        data = (payload or {}).get("data") or {}
        raw_status = str(data.get("status") or "").lower()

        detail: str | None = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            detail = str(errors[0].get("detail") or errors[0].get("title") or "") or None

        if raw_status == "success":
            return OrderStatusResult(status="active")
        if raw_status in ("failed", "cancelled", "canceled"):
            return OrderStatusResult(
                status="failed",
                detail=detail or f"Telnyx order {provider_ref} {raw_status}",
            )
        return OrderStatusResult(status="pending", detail=detail or raw_status or "pending")

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self.api_key}"}
        ref = provider_ref
        if not ref:
            lookup = await client.get(
                f"{self.base_url}/phone_numbers",
                params={"filter[phone_number]": e164},
                headers=headers,
            )
            if lookup.status_code == 200:
                data = (lookup.json() or {}).get("data") or []
                if data and isinstance(data[0], dict):
                    ref = str(data[0].get("id") or "")
        if not ref:
            raise ValidationFailedError(f"Telnyx does not report owning {e164}")

        resp = await client.delete(f"{self.base_url}/phone_numbers/{ref}", headers=headers)
        if resp.status_code not in (200, 202, 204, 404):
            raise ValidationFailedError(
                f"Telnyx refused to release {e164}: {resp.status_code}"
            )
