"""Provider seam for identity verification (KYC).

Everything above this module - the KYC service, the API routes - asks for "the
identity provider" and gets back a small object with one method. Which concrete
provider that is comes from configuration, so swapping Stripe Identity for Didit
(or running both in different environments) is a settings change rather than a
code change.

The seam exists because the two providers disagree on almost every detail that
matters: Stripe collects the person's email itself and returns ``id``/``url``,
while Didit takes the email as an optional contact detail and returns
``session_id``/``url``. Those differences are absorbed here so callers only ever
see ``StartedVerification``.

Secrets are never logged. The only thing this module logs is which provider was
selected, and that is a non-secret name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.errors import ConfigurationError
from app.services import didit_client, stripe_client

log = structlog.get_logger("identity_provider")

#: The provider used when nothing is configured. Stripe is the incumbent, so an
#: unset value must keep behaving exactly as it did before this seam existed.
DEFAULT_PROVIDER = "stripe"

#: Every provider name this module can resolve. Kept as a tuple so the error
#: message below and the dispatch table can never drift apart.
SUPPORTED_PROVIDERS = ("stripe", "didit")


@dataclass(frozen=True)
class StartedVerification:
    """A verification session that has been created at a provider.

    ``session_id`` is the provider's own id, not ours: it is what webhooks and
    status lookups come back keyed on. ``url`` is where the person is sent to
    complete the check.
    """

    provider: str
    session_id: str
    url: str


class IdentityProvider(Protocol):
    """The one operation the rest of the app needs from an identity provider."""

    name: str

    async def start(
        self,
        settings,
        *,
        org_id: uuid.UUID,
        person_id: uuid.UUID,
        email: str | None,
        return_url: str,
    ) -> StartedVerification: ...


class StripeIdentityProvider:
    """Stripe Identity, unchanged from the call the KYC service made before.

    ``email`` is accepted to satisfy the protocol but deliberately not forwarded:
    Stripe collects the person's details inside its own hosted flow, and passing
    an email here would change the request the incumbent provider receives.
    """

    name = "stripe"

    async def start(
        self,
        settings,
        *,
        org_id: uuid.UUID,
        person_id: uuid.UUID,
        email: str | None,
        return_url: str,
    ) -> StartedVerification:
        created = await stripe_client.create_verification_session(
            settings,
            metadata={
                "purpose": "kyc_person",
                "org_id": str(org_id),
                "person_id": str(person_id),
            },
            return_url=return_url,
        )
        return StartedVerification("stripe", created["id"], created["url"])


class DiditIdentityProvider:
    """Didit, which needs our person id as ``vendor_data`` for duplicate detection.

    ``vendor_data`` is the KycPerson id rather than the org id because Didit uses
    it to spot the same human starting a second session; the org is carried in
    ``metadata`` instead, which is echoed back on webhooks.
    """

    name = "didit"

    async def start(
        self,
        settings,
        *,
        org_id: uuid.UUID,
        person_id: uuid.UUID,
        email: str | None,
        return_url: str,
    ) -> StartedVerification:
        created = await didit_client.create_session(
            settings,
            vendor_data=str(person_id),
            metadata={
                "purpose": "kyc_person",
                "org_id": str(org_id),
                "person_id": str(person_id),
            },
            callback=return_url,
            contact_email=email,
        )
        return StartedVerification("didit", created["session_id"], created["url"])


#: Name -> provider instance. Instances are stateless, so one shared copy each is
#: enough and keeps ``get_provider`` allocation-free on the request path.
_PROVIDERS: dict[str, IdentityProvider] = {
    "stripe": StripeIdentityProvider(),
    "didit": DiditIdentityProvider(),
}


def get_provider(settings) -> IdentityProvider:
    """Resolve the configured identity provider.

    An empty or missing setting means Stripe, because that is what the app did
    before this seam existed and a blank value must not break existing installs.
    An unrecognised value is a configuration mistake, not a runtime condition, so
    it raises ``ConfigurationError`` naming the values we do accept rather than
    silently falling back to a provider the operator did not ask for.
    """
    configured = getattr(settings, "kyc_identity_provider", DEFAULT_PROVIDER)
    name = (configured or "").strip().lower() or DEFAULT_PROVIDER

    provider = _PROVIDERS.get(name)
    if provider is None:
        allowed = ", ".join(SUPPORTED_PROVIDERS)
        log.error("identity_provider_unknown", configured=name)
        raise ConfigurationError(
            f"Unknown identity provider {name!r}. Allowed values: {allowed}."
        )

    log.debug("identity_provider_selected", provider=name)
    return provider
