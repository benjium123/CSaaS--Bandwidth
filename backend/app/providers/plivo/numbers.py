"""Plivo number provisioning.

Mixed into the messaging adapter (same reasoning as Telnyx P4): one account, one pair of
credentials, one base URL - splitting provisioning into its own object would just mean two
holders of the same secret.
"""

from __future__ import annotations

import httpx
import structlog

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.numbers import AvailableNumber, NumberSearch, OrderResult

log = structlog.get_logger("carrier.plivo.numbers")


class PlivoNumberProviderMixin:
    """Provisioning half of the Plivo adapter. Requires `_get_client`, `_auth`, `base_url`
    from the composing class (see adapter.py)."""

    async def search_numbers(self, query: NumberSearch) -> list[AvailableNumber]:
        number_type = "tollfree" if query.number_type == "tollfree" else "local"
        params: dict[str, object] = {"country_iso": "US", "type": number_type}
        pattern = query.contains or query.area_code
        if pattern:
            params["pattern"] = pattern
        if query.region:
            params["region"] = query.region

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/PhoneNumber/",
                params=params,
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Plivo unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise FeatureUnavailableError(f"Plivo number search failed with {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        out: list[AvailableNumber] = []
        for item in payload.get("objects") or []:
            if not isinstance(item, dict):
                continue
            number = str(item.get("number") or "")
            if not number:
                continue
            out.append(
                AvailableNumber(
                    e164=number if number.startswith("+") else f"+{number}",
                    number_type=number_type,
                    region=str(item.get("region") or ""),
                    monthly_cost=str(item.get("monthly_rental_rate") or ""),
                    capabilities={
                        "sms": bool(item.get("sms_enabled")),
                        "mms": bool(item.get("mms_enabled")),
                        "voice": bool(item.get("voice_enabled")),
                    },
                )
            )
        return out

    async def order_number(self, e164: str) -> OrderResult:
        national = e164.lstrip("+")
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}/PhoneNumber/{national}/",
                auth=self._auth,
            )
        except httpx.TransportError as exc:
            raise FeatureUnavailableError(f"Plivo unreachable: {exc}") from exc

        if resp.status_code not in (200, 201, 202):
            try:
                detail = str((resp.json() or {}).get("error") or "")
            except ValueError:
                detail = resp.text[:200]
            raise ValidationFailedError(f"Plivo refused the order: {detail or resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        return OrderResult(
            e164=e164,
            provider_ref=str((payload or {}).get("apiId") or (payload or {}).get("api_id") or ""),
            # Plivo's 201 here means "order accepted", checked, not assumed.
            status="active",
        )

    async def release_number(self, e164: str, provider_ref: str | None = None) -> None:
        national = e164.lstrip("+")
        client = await self._get_client()
        resp = await client.delete(
            f"{self.base_url}/Number/{national}/",
            auth=self._auth,
        )
        if resp.status_code not in (200, 202, 204, 404):
            raise ValidationFailedError(f"Plivo refused to release {e164}: {resp.status_code}")
