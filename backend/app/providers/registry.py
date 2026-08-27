"""Every configured carrier, at once.

P1 had `build_carrier(settings) -> one carrier` and stashed it at `app.state.carrier`. That
singular assumption is what phase-3b removes: from here on the app holds a *registry*, and
"which carrier" is a routing decision made per message rather than a deployment constant.

`build_carrier` survives as a shim returning the primary. The P1/P2 seam tests are written
against it, and those tests are the evidence the abstraction held - rewriting them to fit a
newer design would be editing the evidence.
"""

from __future__ import annotations

import structlog

from app.providers.base import MessagingCarrier
from app.providers.health import HealthRegistry

log = structlog.get_logger("carrier.registry")


class CarrierRegistry:
    """Name -> adapter, plus the health view the router reads."""

    def __init__(
        self,
        carriers: dict[str, MessagingCarrier] | None = None,
        *,
        primary: str = "",
        health: HealthRegistry | None = None,
    ) -> None:
        self._carriers: dict[str, MessagingCarrier] = dict(carriers or {})
        self._primary = primary
        self.health = health or HealthRegistry()

    def __contains__(self, name: object) -> bool:
        return name in self._carriers

    def __len__(self) -> int:
        return len(self._carriers)

    def names(self) -> list[str]:
        return list(self._carriers)

    def get(self, name: str) -> MessagingCarrier | None:
        return self._carriers.get(name)

    def primary(self) -> MessagingCarrier | None:
        if self._primary and self._primary in self._carriers:
            return self._carriers[self._primary]
        return next(iter(self._carriers.values()), None)

    @property
    def primary_name(self) -> str:
        carrier = self.primary()
        return carrier.name if carrier else ""

    def healthy_names(self) -> list[str]:
        return [n for n in self._carriers if self.health.is_healthy(n)]

    async def aclose(self) -> None:
        for carrier in self._carriers.values():
            closer = getattr(carrier, "aclose", None)
            if closer is not None:
                await closer()

    def status(self) -> list[dict]:
        """Operator-facing view. Never includes a credential."""
        snapshot = self.health.snapshot()
        return [
            {
                "name": name,
                "primary": name == self.primary_name,
                "state": snapshot.get(name, {}).get("state", "closed"),
                "consecutive_failures": snapshot.get(name, {}).get("consecutive_failures", 0),
                "capabilities": {
                    "supports_cancel": carrier.capabilities.supports_cancel,
                    "supports_scheduled_send": carrier.capabilities.supports_scheduled_send,
                    "max_media_bytes": carrier.capabilities.max_media_bytes,
                },
            }
            for name, carrier in self._carriers.items()
        ]


def build_registry(settings) -> CarrierRegistry:  # noqa: ANN001
    """Build every carrier the environment actually has credentials for.

    Never raises: a missing carrier is a *capability* the deployment lacks, not a boot
    failure. `/healthz` and the provider-status endpoint are how an operator finds out,
    and they name the missing VARIABLES, never the values.
    """
    carriers: dict[str, MessagingCarrier] = {}

    if getattr(settings, "loopback_carrier_enabled", False):
        # The config validator has already refused production and the both-on case, so by
        # the time we are here loopback is exclusive on purpose.
        from app.providers.loopback import LoopbackCarrier

        carriers["loopback"] = LoopbackCarrier()
        return CarrierRegistry(carriers, primary="loopback")

    statuses = {p.name: p for p in settings.provider_statuses()}

    def enabled(name: str) -> bool:
        status = statuses.get(name)
        return bool(status and status.enabled)

    # Voice and messaging are separate Bandwidth products with separate application ids.
    # Requiring the MESSAGING id to build the adapter meant a voice-only account got no
    # carrier at all - not even for calls it was perfectly entitled to make.
    if enabled("bandwidth") and (
        settings.bandwidth_messaging_application_id.strip()
        or settings.bandwidth_voice_application_id.strip()
    ):
        from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

        carriers["bandwidth"] = BandwidthMessagingCarrier(
            account_id=settings.bandwidth_account_id,
            api_username=settings.bandwidth_api_username,
            api_password=settings.bandwidth_api_password.get_secret_value(),
            application_id=settings.bandwidth_messaging_application_id,
            webhook_username=settings.bandwidth_webhook_username,
            webhook_password=settings.bandwidth_webhook_password.get_secret_value(),
        )
        bw = carriers["bandwidth"]
        # Voice attributes ride on the same adapter object; the voice mixin reads them
        # via getattr so the messaging constructor (a frozen P1 seam) stays untouched.
        bw.voice_application_id = settings.bandwidth_voice_application_id
        bw.voice_callback_url = (
            settings.public_base_url.rstrip("/") + "/api/v1/webhooks/bandwidth/voice"
            if settings.public_base_url
            else ""
        )
        bw.voice_webhook_username = settings.bandwidth_webhook_username
        bw.voice_webhook_password = settings.bandwidth_webhook_password.get_secret_value()

    if enabled("telnyx"):
        from app.providers.telnyx.adapter import TelnyxMessagingCarrier

        carriers["telnyx"] = TelnyxMessagingCarrier(
            api_key=settings.telnyx_api_key.get_secret_value(),
            messaging_profile_id=settings.telnyx_messaging_profile_id,
            public_key=settings.telnyx_public_key.get_secret_value(),
        )
        carriers["telnyx"].voice_connection_id = settings.telnyx_voice_connection_id

    if enabled("twilio"):
        from app.providers.twilio.adapter import TwilioMessagingCarrier

        carriers["twilio"] = TwilioMessagingCarrier(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token.get_secret_value(),
            messaging_service_sid=settings.twilio_messaging_service_sid,
            webhook_url=settings.twilio_webhook_url,
        )

    if enabled("plivo"):
        from app.providers.plivo.adapter import PlivoMessagingCarrier

        carriers["plivo"] = PlivoMessagingCarrier(
            auth_id=settings.plivo_auth_id,
            auth_token=settings.plivo_auth_token.get_secret_value(),
            powerpack_uuid=settings.plivo_powerpack_uuid,
            webhook_url=settings.plivo_webhook_url,
        )

    if enabled("signalwire"):
        from app.providers.signalwire.adapter import SignalWireMessagingCarrier

        carriers["signalwire"] = SignalWireMessagingCarrier(
            project_id=settings.signalwire_project_id,
            api_token=settings.signalwire_api_token.get_secret_value(),
            space_url=settings.signalwire_space_url,
            webhook_url=settings.signalwire_webhook_url,
        )

    # Preference order when an org has expressed none. Bandwidth first is the user's
    # stated default, not a technical claim.
    primary = next((n for n in ("bandwidth", "telnyx", "signalwire") if n in carriers), "")
    if carriers:
        log.info("carrier_registry_built", carriers=sorted(carriers), primary=primary)
    else:
        log.warning("carrier_registry_empty", reason="no carrier has complete credentials")
    return CarrierRegistry(carriers, primary=primary)
