from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.provider_accounts import (
    PROVIDER_CREDENTIAL_FIELDS,
    PROVIDER_NAMES,
    ProviderAccount,
)
from app.providers import probes
from app.services import credentials as credential_svc

_ORG_ACCOUNT_VERSIONS: dict[uuid.UUID, int] = {}


def bump_version(org_id: uuid.UUID) -> int:
    _ORG_ACCOUNT_VERSIONS[org_id] = _ORG_ACCOUNT_VERSIONS.get(org_id, 0) + 1
    return _ORG_ACCOUNT_VERSIONS[org_id]


def current_version(org_id: uuid.UUID) -> int:
    return _ORG_ACCOUNT_VERSIONS.get(org_id, 0)


def _empty(v: object) -> bool:
    if v is None:
        return True
    if hasattr(v, "get_secret_value"):
        return not v.get_secret_value().strip()
    return not str(v).strip()


def _carrier_requirements_for(s: object) -> dict[str, dict[str, object]]:
    return {
        "bandwidth": {
            "BANDWIDTH_ACCOUNT_ID": getattr(s, "bandwidth_account_id", ""),
            "BANDWIDTH_API_USERNAME": getattr(s, "bandwidth_api_username", ""),
            "BANDWIDTH_API_PASSWORD": getattr(s, "bandwidth_api_password", ""),
        },
        "telnyx": {
            "TELNYX_API_KEY": getattr(s, "telnyx_api_key", ""),
        },
        "twilio": {
            "TWILIO_ACCOUNT_SID": getattr(s, "twilio_account_sid", ""),
            "TWILIO_AUTH_TOKEN": getattr(s, "twilio_auth_token", ""),
        },
        "plivo": {
            "PLIVO_AUTH_ID": getattr(s, "plivo_auth_id", ""),
            "PLIVO_AUTH_TOKEN": getattr(s, "plivo_auth_token", ""),
        },
        "signalwire": {
            "SIGNALWIRE_PROJECT_ID": getattr(s, "signalwire_project_id", ""),
            "SIGNALWIRE_API_TOKEN": getattr(s, "signalwire_api_token", ""),
            "SIGNALWIRE_SPACE_URL": getattr(s, "signalwire_space_url", ""),
        },
    }


def _carrier_live_for(s: object, name: str) -> bool:
    flag = getattr(s, f"{name}_enabled", None)
    if flag is False:
        return False
    required = _carrier_requirements_for(s).get(name, {})
    return not [k for k, v in required.items() if _empty(v)]


#: Presence marker mask() returns for a set secret field - MUST match exactly, both here
#: and in mask() below, or the round-trip guard in validate_credentials() silently stops
#: recognizing it.
MASKED_SECRET_VALUE = "•••••"

#: Bandwidth's pair beyond what config.py's carrier_requirements()/carrier_live() treat
#: as required for the credential to be "live": config.py's own is_production validator
#: separately fails closed on these ("BANDWIDTH_WEBHOOK_USERNAME / ..._PASSWORD are
#: required when Bandwidth is enabled - voice webhooks fail closed without them"), and
#: the P17 webhook DB-account fallback (app/api/routes/webhooks.py) depends on them never
#: being blank - an empty configured Basic-auth pair matches an equally-empty request
#: (see webhooks.py's bandwidth_messaging guard). messaging_application_id is required
#: too (a bandwidth account defaults to being an SMS account); voice_application_id alone
#: is the one bandwidth field genuinely optional at creation (add voice later via PATCH).
_EXTRA_REQUIRED_FIELDS: dict[str, set[str]] = {
    "bandwidth": {"messaging_application_id", "webhook_username", "webhook_password"},
}

#: SSRF guard: signalwire_space_url is interpolated directly into a request URL by both
#: the probe (app/providers/probes.py::_probe_signalwire) and the live adapter
#: (SignalWireMessagingCarrier) - an org admin (or a compromised admin session) able to
#: set it to an arbitrary host turns the probe/send path into an outbound request to
#: anywhere THIS SERVER can reach (cloud metadata endpoints, internal services), carrying
#: that org's own api_token in the request. Restricting it to a bare SignalWire space
#: hostname closes that off; no scheme, path, port, or credentials are ever accepted.
_SIGNALWIRE_SPACE_URL_RE = re.compile(r"^[a-z0-9-]+\.signalwire\.com$", re.IGNORECASE)


def _required_field_names(provider: str) -> set[str]:
    """The credential fields create_account cannot do without - the SAME requirement set
    config.py's Settings.carrier_requirements() enforces for the env path, mapped from
    ``{PROVIDER}_{FIELD}`` env-var names down to bare field names, plus
    _EXTRA_REQUIRED_FIELDS above. Every other PROVIDER_CREDENTIAL_FIELDS entry (telnyx's
    public_key, plivo's powerpack_uuid, bandwidth's voice_application_id, ...) may be
    omitted at creation and added later via PATCH."""
    prefix = f"{provider.upper()}_"
    dummy = SimpleNamespace()
    required = {
        env_key[len(prefix):].lower()
        for env_key in _carrier_requirements_for(dummy).get(provider, {})
        if env_key.startswith(prefix)
    }
    required |= _EXTRA_REQUIRED_FIELDS.get(provider, set())
    return required


def validate_credentials(provider: str, data: dict, *, partial: bool = False) -> dict:
    if provider not in PROVIDER_NAMES:
        raise ValidationFailedError(f"Unknown provider: {provider}")

    fields = PROVIDER_CREDENTIAL_FIELDS[provider]
    unknown = set(data) - set(fields)
    if unknown:
        raise ValidationFailedError(
            f"Unknown credential field for {provider}: {sorted(unknown)[0]}"
        )

    cleaned: dict = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailedError(f"{key} must be a non-empty string")
        if fields[key] and value == MASKED_SECRET_VALUE:
            # A secret field carrying back the exact presence-marker mask() hands out on
            # GET is a client round-tripping a response it never meant to change - not a
            # real credential (a masked value cannot be typed by accident: it is five
            # bullet characters, never a valid API key/token).
            if partial:
                continue  # unchanged: PATCH keeps whatever is already stored
            raise ValidationFailedError(
                f"{key} looks like a masked placeholder, not a real credential"
            )
        if (
            provider == "signalwire"
            and key == "space_url"
            and not _SIGNALWIRE_SPACE_URL_RE.fullmatch(value.strip())
        ):
            raise ValidationFailedError(
                "space_url must be a bare SignalWire space hostname, e.g. "
                "my-space.signalwire.com - no scheme, path, port, or credentials"
            )
        cleaned[key] = value

    if not partial:
        required = _required_field_names(provider)
        missing = [k for k in required if k not in cleaned]
        if missing:
            raise ValidationFailedError(
                f"Missing required credential fields for {provider}: {sorted(missing)}"
            )

    return cleaned


def _invalidate_webhook_account_cache(provider: str) -> None:
    """Drop app/api/routes/webhooks.py's short-TTL cache of "active accounts for this
    provider" so a credential rotation, probe result, or disable is picked up on the
    very next webhook delivery rather than up to _WEBHOOK_ACCOUNTS_TTL_SECONDS late.

    Imported lazily (at call time, not module load time): webhooks.py imports
    app.providers.registry_org, which imports THIS module for current_version/
    settings_like_for - a module-level import here of webhooks.py would be a real import
    cycle, not just an unusual dependency direction.
    """
    from app.api.routes import webhooks as webhooks_routes

    webhooks_routes._WEBHOOK_ACCOUNTS_CACHE.pop(provider, None)


def mask(provider: str, data: dict) -> dict:
    fields = PROVIDER_CREDENTIAL_FIELDS[provider]
    out: dict[str, str] = {}
    for field_name, is_secret in fields.items():
        value = data.get(field_name) or ""
        if is_secret:
            out[field_name] = MASKED_SECRET_VALUE if value else ""
        else:
            out[field_name] = value or ""
    return out


async def create_account(
    session: AsyncSession,
    settings: Settings,
    *,
    org_id: uuid.UUID,
    provider: str,
    label: str,
    credentials: dict,
    actor_user_id: uuid.UUID | None,
) -> ProviderAccount:
    cleaned = validate_credentials(provider, credentials)
    encrypted = credential_svc.encrypt(settings, cleaned)

    # provider_accounts has uq_provider_accounts_org_provider (org_id, provider) - one
    # account per provider per org in P17. A prior disable() left status="disabled"
    # rather than deleting the row (disable_account never hard-deletes), so re-adding the
    # SAME provider must revive that row rather than 500 on the unique-constraint clash a
    # blind INSERT would hit.
    existing = (
        await session.execute(
            sa.select(ProviderAccount).where(
                ProviderAccount.org_id == org_id, ProviderAccount.provider == provider
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status != "disabled":
            raise ConflictError(
                f"A provider account for {provider} already exists for this org "
                "- disable it first, or PATCH it instead of creating a new one"
            )
        existing.credentials_encrypted = encrypted
        existing.label = label.strip() if label else ""
        # Back to unverified, same as a brand-new row: the revived credentials have
        # never been probed and must not be trusted "active" until they are.
        existing.status = "unverified"
        existing.last_probe_at = None
        existing.last_probe_detail = None
        existing.created_by = actor_user_id
        bump_version(org_id)
        return existing

    account = ProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        provider=provider,
        label=label.strip() if label else "",
        credentials_encrypted=encrypted,
        status="unverified",
        created_by=actor_user_id,
    )
    session.add(account)
    bump_version(org_id)
    return account


async def update_account(
    session: AsyncSession,
    settings: Settings,
    account: ProviderAccount,
    *,
    label: str | None = None,
    credentials: dict | None = None,
) -> ProviderAccount:
    if label is not None:
        account.label = label.strip()

    if credentials is not None:
        cleaned = validate_credentials(account.provider, credentials, partial=True)
        stored = credential_svc.decrypt(settings, account.credentials_encrypted)
        merged = {**stored, **cleaned}
        account.credentials_encrypted = credential_svc.encrypt(settings, merged)

    if account.org_id is not None:
        bump_version(account.org_id)
    _invalidate_webhook_account_cache(account.provider)
    return account


async def disable_account(session: AsyncSession, account: ProviderAccount) -> ProviderAccount:
    account.status = "disabled"
    if account.org_id is not None:
        bump_version(account.org_id)
    _invalidate_webhook_account_cache(account.provider)
    return account


async def list_accounts(session: AsyncSession) -> list[ProviderAccount]:
    return list((await session.execute(sa.select(ProviderAccount))).scalars().all())


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> ProviderAccount:
    account = await session.get(ProviderAccount, account_id)
    if account is None:
        raise NotFoundError("Provider account not found")
    return account


def settings_like_for(settings: Settings, account: ProviderAccount) -> object:
    creds = credential_svc.decrypt(settings, account.credentials_encrypted)

    ns = SimpleNamespace()
    for field_name in settings.model_fields:
        setattr(ns, field_name, getattr(settings, field_name))

    for key, value in vars(settings).items():
        if not key.startswith("_") and key not in settings.model_fields:
            try:
                setattr(ns, key, value)
            except Exception:
                pass

    for field_name, is_secret in PROVIDER_CREDENTIAL_FIELDS[account.provider].items():
        attr = f"{account.provider}_{field_name}"
        value = creds.get(field_name, "")
        if is_secret:
            setattr(ns, attr, SecretStr(value))
        else:
            setattr(ns, attr, value)

    setattr(ns, f"{account.provider}_enabled", True)

    # Methods used by probes.py and the per-org registry builder. They must read the
    # overridden attributes on ns, not the original Settings instance.
    ns.carrier_flag = lambda name: getattr(ns, f"{name}_enabled", None)  # type: ignore[attr-defined]
    ns.carrier_requirements = lambda: _carrier_requirements_for(ns)  # type: ignore[attr-defined]
    ns.carrier_live = lambda name: _carrier_live_for(ns, name)  # type: ignore[attr-defined]

    return ns


async def probe_account(
    session: AsyncSession,
    settings: Settings,
    account: ProviderAccount,
    *,
    client: httpx.AsyncClient | None = None,
) -> ProviderAccount:
    result = await probes.probe(
        account.provider, settings_like_for(settings, account), client=client
    )
    account.last_probe_at = datetime.now(timezone.utc)
    account.last_probe_detail = (result.detail or "")[:512]
    account.status = "active" if result.ok else "failed"

    if account.org_id is not None:
        bump_version(account.org_id)
    _invalidate_webhook_account_cache(account.provider)
    return account
