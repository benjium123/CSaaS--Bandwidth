"""Twilio number provisioning.

Mixed into the messaging adapter rather than living beside it, for the same reason as
Telnyx (`telnyx/numbers.py`): on Twilio, messaging and provisioning share one account SID /
auth token, and splitting them into two objects would mean two holders of the same
credential for no gain.
"""

from __future__ import annotations

import httpx
import structlog

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult

log = structlog.get_logger("carrier.twilio.numbers")


def _capabilities(caps: object) -> dict:
    """Twilio reports capabilities as ``{"voice": true, "SMS": true, "MMS": true}`` -
    key-casing is inconsistent across endpoints, so both cases are checked."""
    if not isinstance(caps, dict):
        return {}
    return {
        "sms": bool(caps.get("sms") or caps.get("SMS")),
        "mms": bool(caps.get("mms") or caps.get("MMS")),
        "voice": bool(caps.get("voice") or caps.get("Voice")),
    }


class TwilioNumberProviderMixin:
    """Provisioning half of the Twilio adapter. Requires `_get_client`, `base_url`, `_auth`."""

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
        resp = await client.get(
            f"{self.base_url}/AvailablePhoneNumbers/US/{kind}.json",
            params=params,
            auth=self._auth,
        )
        if resp.status_code != 200:
            raise FeatureUnavailableError(
                f"Twilio number search failed with {resp.status_code}"
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
                )
            )
        return [n for n in out if n.e164]

    async def order_number(self, e164: str) -> OrderResult:
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/IncomingPhoneNumbers.json",
                data={"PhoneNumber": e164},
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Twilio unreachable: {exc}") from exc

        if resp.status_code not in (200, 201):
            detail = ""
            try:
                payload = resp.json()
                detail = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            except ValueError:
                detail = resp.text[:200]
            raise ValidationFailedError(
                f"Twilio refused the order: {detail or resp.status_code}"
            )

        payload = resp.json() or {}
        return OrderResult(
            e164=str(payload.get("phone_number") or e164),
            provider_ref=str(payload.get("sid") or ""),
            # The create-IncomingPhoneNumber call is synchronous on Twilio - by the time it
            # returns 2xx the number is routable, unlike Telnyx's async order flow.
            status="active",
            capabilities=_capabilities(payload.get("capabilities")),
        )

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        client = await self._get_client()
        ref = provider_ref
        if not ref:
            lookup = await client.get(
                f"{self.base_url}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": e164},
                auth=self._auth,
            )
            if lookup.status_code == 200:
                entries = (lookup.json() or {}).get("incoming_phone_numbers") or []
                if entries and isinstance(entries[0], dict):
                    ref = str(entries[0].get("sid") or "")
        if not ref:
            raise ValidationFailedError(f"Twilio does not report owning {e164}")

        resp = await client.delete(
            f"{self.base_url}/IncomingPhoneNumbers/{ref}.json", auth=self._auth
        )
        if resp.status_code not in (200, 202, 204, 404):
            raise ValidationFailedError(
                f"Twilio refused to release {e164}: {resp.status_code}"
            )
