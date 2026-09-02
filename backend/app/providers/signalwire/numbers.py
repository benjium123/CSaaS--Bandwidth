"""SignalWire number provisioning.

SignalWire's Compatibility API is Twilio-shaped, so this mixin intentionally mirrors
`twilio/numbers.py`. The only meaningful differences are the base URL (which the messaging
adapter already owns) and the credentials (project_id / api_token).
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult


def _capabilities(caps: object) -> dict:
    """SignalWire reports Twilio-style capabilities with inconsistent key casing."""
    if not isinstance(caps, dict):
        return {}
    return {
        "sms": bool(caps.get("sms") or caps.get("SMS")),
        "mms": bool(caps.get("mms") or caps.get("MMS")),
        "voice": bool(caps.get("voice") or caps.get("Voice")),
    }


class SignalWireNumberProviderMixin:
    """Provisioning half of the SignalWire adapter.

    Requires `_get_client`, `project_id`, `_api_token`, and `base_url` from the composing
    class (see `signalwire/adapter.py`).
    """

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        kind = "TollFree" if query.number_type == "tollfree" else "Local"
        params: dict[str, object] = {"PageSize": min(max(query.limit, 1), 100)}
        if query.area_code:
            params["AreaCode"] = query.area_code
        if query.contains:
            params["Contains"] = query.contains
        if query.region:
            params["InRegion"] = query.region
        if query.locality:
            params["InLocality"] = query.locality

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/AvailablePhoneNumbers/US/{kind}.json",
                params=params,
                auth=(self.project_id, self._api_token),
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"SignalWire unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"SignalWire number search failed with {resp.status_code}"
            )

        payload = resp.json()
        out: list[AvailableNumber] = []
        for item in payload.get("available_phone_numbers") or []:
            if not isinstance(item, dict):
                continue
            out.append(
                AvailableNumber(
                    e164=str(item.get("phone_number") or ""),
                    number_type="tollfree" if kind == "TollFree" else "local",
                    region=str(item.get("region") or ""),
                    locality=str(item.get("locality") or ""),
                    monthly_cost="",
                    setup_cost="",
                    capabilities=_capabilities(item.get("capabilities")),
                    monthly_cost_cents=None,
                    setup_cost_cents=None,
                )
            )
        return [n for n in out if n.e164]

    async def order_number(self, e164: str) -> OrderResult:
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/IncomingPhoneNumbers.json",
                data={"PhoneNumber": e164},
                auth=(self.project_id, self._api_token),
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"SignalWire unreachable: {exc}") from exc

        if resp.status_code not in (200, 201):
            detail = ""
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("message") or "")
            except ValueError:
                detail = resp.text[:200]
            raise ValidationFailedError(
                f"SignalWire refused the order: {detail or resp.status_code}"
            )

        payload = resp.json() or {}
        return OrderResult(
            e164=str(payload.get("phone_number") or e164),
            provider_ref=str(payload.get("sid") or ""),
            # Create IncomingPhoneNumber is synchronous on this API.
            status="active",
            capabilities=_capabilities(payload.get("capabilities")),
            monthly_cost_cents=None,
            setup_cost_cents=None,
        )

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        client = await self._get_client()
        auth = (self.project_id, self._api_token)

        ref = provider_ref
        if not ref:
            lookup = await client.get(
                f"{self.base_url}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": e164},
                auth=auth,
            )
            if lookup.status_code == 200:
                entries = (lookup.json() or {}).get("incoming_phone_numbers") or []
                if entries and isinstance(entries[0], dict):
                    ref = str(entries[0].get("sid") or "")
        if not ref:
            raise ValidationFailedError(f"SignalWire does not report owning {e164}")

        resp = await client.delete(
            f"{self.base_url}/IncomingPhoneNumbers/{quote(ref, safe='')}.json",
            auth=auth,
        )
        if resp.status_code not in (200, 202, 204, 404):
            raise ValidationFailedError(
                f"SignalWire refused to release {e164}: {resp.status_code}"
            )
