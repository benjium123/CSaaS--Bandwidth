from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.provider_accounts import PROVIDER_NAMES, ProviderAccount
from app.providers.base import MessagingCarrier
from app.providers.registry import CarrierRegistry, build_registry
from app.services.provider_accounts import current_version, settings_like_for

CURRENT_ORG_ID: ContextVar[uuid.UUID | None] = ContextVar("org_id", default=None)
#: Set for the lifetime of one request by app/auth/deps.py::get_current_org, reset in
#: its `finally`. asyncio.create_task() COPIES the calling context at the moment the task
#: is created (contextvars.copy_context() semantics) - a task spawned mid-request would
#: therefore inherit THAT request's org id into a coroutine that keeps running (and may
#: still be resolving app.state.carriers) after the request that set it has already
#: returned and reset it back to None on ITS OWN context, not the task's copy. Any
#: asyncio.create_task() started from inside a request path in a file P17 owns MUST
#: explicitly do `registry_org.CURRENT_ORG_ID.set(None)` as the first line of the spawned
#: coroutine to avoid running with a stale/borrowed org identity. As of this phase, the
#: only asyncio.create_task() in a P17-owned file is app/main.py's sweeper_task, created
#: ONCE from `lifespan` at process startup - before any request has ever set this
#: contextvar - so it is unaffected; it is not "from a request path" and needs no reset.

#: (org_id, version) -> (registry, adapters that were FRESHLY built from DB credentials
#: for this entry (never the shared env-configured objects), wall-clock time.monotonic()
#: this entry was primed). Only the second element may ever be closed on
#: eviction/invalidation - see build_registry_for_org.
_OrgCacheEntry = tuple[CarrierRegistry, dict[str, MessagingCarrier], float]
_ORG_REGISTRY_CACHE: "OrderedDict[tuple[uuid.UUID, int], _OrgCacheEntry]" = OrderedDict()
_CACHE_MAX = 256

#: Multi-worker staleness backstop. bump_version() (app/services/provider_accounts.py)
#: is an in-process counter - it does not fan out to sibling worker processes - so a
#: worker that never itself handled the create/patch/probe/disable request would
#: otherwise keep serving a stale DB-backed adapter indefinitely. Past this TTL,
#: is_primed()/CarrierRegistryProxy._resolve() both fall back to the global registry
#: (self-healing on the next auth'd request, which re-primes) rather than trust an entry
#: this old.
_ORG_REGISTRY_TTL_SECONDS = 300.0

#: account.id -> (org's version when built, adapter). Backs the webhook DB-account
#: verification fallback (app/api/routes/webhooks.py::_db_account_carrier_verifying) so
#: repeated deliveries for the same account do not rebuild (and immediately discard) an
#: adapter - and, per _OrgCacheEntry above, only ever holds adapters this module built
#: itself, so closing one here is always safe.
_ACCOUNT_CARRIER_CACHE: dict[uuid.UUID, tuple[int, MessagingCarrier]] = {}
_ACCOUNT_CARRIER_CACHE_MAX = 512


async def _close_carrier(carrier: MessagingCarrier | None) -> None:
    if carrier is None:
        return
    closer = getattr(carrier, "aclose", None)
    if closer is not None:
        await closer()


async def _evict_org_cache_entry(key: tuple[uuid.UUID, int]) -> None:
    entry = _ORG_REGISTRY_CACHE.pop(key, None)
    if entry is None:
        return
    _registry, db_owned, _cached_at = entry
    for carrier in db_owned.values():
        await _close_carrier(carrier)


async def _cache_org_registry(
    org_id: uuid.UUID,
    version: int,
    registry: CarrierRegistry,
    db_owned: dict[str, MessagingCarrier],
) -> None:
    # A re-prime of the SAME (org_id, version) - e.g. two requests racing to prime a cold
    # cache - must close whichever adapter set loses the race, not leak it.
    await _evict_org_cache_entry((org_id, version))
    _ORG_REGISTRY_CACHE[(org_id, version)] = (registry, db_owned, time.monotonic())
    while len(_ORG_REGISTRY_CACHE) > _CACHE_MAX:
        oldest_key = next(iter(_ORG_REGISTRY_CACHE))
        await _evict_org_cache_entry(oldest_key)


def _construct_provider(name: str, src: Any, base: Settings) -> MessagingCarrier | None:
    if name == "bandwidth":
        if src.carrier_live("bandwidth") and (
            getattr(src, "bandwidth_messaging_application_id", "").strip()
            or getattr(src, "bandwidth_voice_application_id", "").strip()
        ):
            from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

            carrier = BandwidthMessagingCarrier(
                account_id=getattr(src, "bandwidth_account_id", ""),
                api_username=getattr(src, "bandwidth_api_username", ""),
                api_password=getattr(src, "bandwidth_api_password").get_secret_value(),
                application_id=getattr(src, "bandwidth_messaging_application_id", ""),
                auth_mode=getattr(base, "bandwidth_auth_mode", "oauth2"),
                webhook_username=getattr(src, "bandwidth_webhook_username", ""),
                webhook_password=getattr(src, "bandwidth_webhook_password").get_secret_value(),
            )
            carrier.voice_application_id = getattr(src, "bandwidth_voice_application_id", "")
            carrier.voice_callback_url = (
                base.public_base_url.rstrip("/") + "/api/v1/webhooks/bandwidth/voice"
                if base.public_base_url
                else ""
            )
            carrier.voice_webhook_username = getattr(
                src, "bandwidth_webhook_username", ""
            )
            carrier.voice_webhook_password = getattr(
                src, "bandwidth_webhook_password"
            ).get_secret_value()
            return carrier

    if name == "telnyx" and src.carrier_live("telnyx"):
        from app.providers.telnyx.adapter import TelnyxMessagingCarrier

        carrier = TelnyxMessagingCarrier(
            api_key=getattr(src, "telnyx_api_key").get_secret_value(),
            messaging_profile_id=getattr(src, "telnyx_messaging_profile_id", ""),
            public_key=getattr(src, "telnyx_public_key").get_secret_value(),
        )
        carrier.voice_connection_id = getattr(src, "telnyx_voice_connection_id", "")
        return carrier

    if name == "twilio" and src.carrier_live("twilio"):
        from app.providers.twilio.adapter import TwilioMessagingCarrier

        return TwilioMessagingCarrier(
            account_sid=getattr(src, "twilio_account_sid", ""),
            auth_token=getattr(src, "twilio_auth_token").get_secret_value(),
            messaging_service_sid=getattr(src, "twilio_messaging_service_sid", ""),
            webhook_url=base.twilio_webhook_url,
        )

    if name == "plivo" and src.carrier_live("plivo"):
        from app.providers.plivo.adapter import PlivoMessagingCarrier

        return PlivoMessagingCarrier(
            auth_id=getattr(src, "plivo_auth_id", ""),
            auth_token=getattr(src, "plivo_auth_token").get_secret_value(),
            powerpack_uuid=getattr(src, "plivo_powerpack_uuid", ""),
            webhook_url=base.plivo_webhook_url,
        )

    if name == "signalwire" and src.carrier_live("signalwire"):
        from app.providers.signalwire.adapter import SignalWireMessagingCarrier

        return SignalWireMessagingCarrier(
            project_id=getattr(src, "signalwire_project_id", ""),
            api_token=getattr(src, "signalwire_api_token").get_secret_value(),
            space_url=getattr(src, "signalwire_space_url", ""),
            webhook_url=base.signalwire_webhook_url,
        )

    return None


def build_registry_for_org(
    settings: Settings,
    accounts: list[ProviderAccount],
    *,
    global_registry: CarrierRegistry | None = None,
) -> tuple[CarrierRegistry, dict[str, MessagingCarrier]]:
    """The org's registry, plus the subset of it this call freshly built from DB creds.

    Every provider this org has no ACTIVE account for falls back to ``global_registry``'s
    adapter - the SAME object, not a rebuilt equivalent - and the whole result shares
    ``global_registry``'s HealthRegistry, so a circuit-breaker trip against a
    shared/env-configured carrier is the identical fact whether it is observed through
    this org's registry or the global one. Only ``global_registry`` is None (tests, or a
    caller with no live app instance) does this fall back to building a throwaway env
    registry from ``settings`` instead.

    The second return value is EXACTLY the adapters this call constructed for an active
    DB account - never anything from ``global_registry`` - so the cache above can close
    that set on eviction/invalidation without ever touching a shared env adapter.
    """
    env_registry = global_registry if global_registry is not None else build_registry(settings)
    carriers: dict[str, MessagingCarrier] = {}
    for name in env_registry.names():
        carrier = env_registry.get(name)
        if carrier is not None:
            carriers[name] = carrier

    db_owned: dict[str, MessagingCarrier] = {}
    for account in accounts:
        if account.status != "active" or account.provider not in PROVIDER_NAMES:
            continue
        adapter = _construct_provider(
            account.provider, settings_like_for(settings, account), settings
        )
        if adapter is not None:
            carriers[account.provider] = adapter
            db_owned[account.provider] = adapter

    primary = next((n for n in ("bandwidth", "telnyx", "signalwire") if n in carriers), "")
    registry = CarrierRegistry(carriers, primary=primary, health=env_registry.health)
    return registry, db_owned


def is_primed(org_id: uuid.UUID) -> bool:
    """True when the per-org registry for this org's CURRENT version is cached AND still
    inside the TTL backstop.

    The cheap path for app/auth/deps.py: skip the ``provider_accounts`` query on most
    requests and only hit the DB via ``prime_org_registry`` the first time an org is seen,
    again after its accounts change (create/patch/probe/disable bump the version via
    ``app.services.provider_accounts.bump_version``), and again once the TTL backstop
    above lapses.
    """
    entry = _ORG_REGISTRY_CACHE.get((org_id, current_version(org_id)))
    if entry is None:
        return False
    _registry, _db_owned, cached_at = entry
    return time.monotonic() - cached_at <= _ORG_REGISTRY_TTL_SECONDS


async def carrier_for_account(settings: Settings, account: ProviderAccount) -> MessagingCarrier | None:
    """One adapter for ONE account's decrypted credentials, cached per (account id, its
    org's current version) and closed the moment that version moves on.

    Used only by the webhook DB-account verification fallback
    (app/api/routes/webhooks.py) - it never touches the org-registry cache above, since a
    webhook has no org context yet (that verification IS how one gets established).
    """
    version = current_version(account.org_id) if account.org_id is not None else 0
    cached = _ACCOUNT_CARRIER_CACHE.get(account.id)
    if cached is not None and cached[0] == version:
        return cached[1]
    if cached is not None:
        await _close_carrier(cached[1])
        _ACCOUNT_CARRIER_CACHE.pop(account.id, None)

    carrier = _construct_provider(account.provider, settings_like_for(settings, account), settings)
    if carrier is None:
        return None

    _ACCOUNT_CARRIER_CACHE[account.id] = (version, carrier)
    while len(_ACCOUNT_CARRIER_CACHE) > _ACCOUNT_CARRIER_CACHE_MAX:
        oldest_id = next(iter(_ACCOUNT_CARRIER_CACHE))
        _, oldest_carrier = _ACCOUNT_CARRIER_CACHE.pop(oldest_id)
        await _close_carrier(oldest_carrier)
    return carrier


async def prime_org_registry(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    *,
    global_registry: CarrierRegistry | None = None,
) -> CarrierRegistry:
    rows = list(
        (
            await session.execute(
                sa.select(ProviderAccount).where(
                    ProviderAccount.org_id == org_id, ProviderAccount.status == "active"
                )
            )
        )
        .scalars()
        .all()
    )
    registry, db_owned = build_registry_for_org(settings, rows, global_registry=global_registry)
    await _cache_org_registry(org_id, current_version(org_id), registry, db_owned)
    return registry


class CarrierRegistryProxy:
    """Wraps the global (env-configured) CarrierRegistry so that a request bound to an
    org with a primed, active DB-account registry (app/auth/deps.py) transparently sees
    that instead - and every existing caller with no org context (startup, the sweeper,
    webhooks, and every route/test that never runs through get_current_org) resolves
    straight through to the SAME global registry as before this proxy existed.

    __len__/__contains__/__iter__/__bool__ must be defined explicitly: Python looks those
    up on the TYPE, never through __getattr__, so without them `len(app.state.carriers)`
    and `x in app.state.carriers` (app/routing/router.py, app/api/routes/numbers.py)
    would silently break the moment this wraps a raw CarrierRegistry - __getattr__ alone
    (the pre-review version of this class) only ever covered ordinary attribute/method
    access such as `.get()`, `.health`, `.status()`.
    """

    def __init__(self, global_registry: CarrierRegistry) -> None:
        self._global_registry = global_registry

    @property
    def global_registry(self) -> CarrierRegistry:
        """The env-configured registry with NO org context applied. Used by
        app/auth/deps.py to prime a per-org registry that shares this one's adapter
        objects and HealthRegistry instead of rebuilding both from scratch."""
        return self._global_registry

    def _resolve(self) -> CarrierRegistry:
        org_id = CURRENT_ORG_ID.get()
        if org_id is None:
            return self._global_registry
        entry = _ORG_REGISTRY_CACHE.get((org_id, current_version(org_id)))
        if entry is None:
            return self._global_registry
        registry, _db_owned, cached_at = entry
        if time.monotonic() - cached_at > _ORG_REGISTRY_TTL_SECONDS:
            return self._global_registry
        return registry

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __len__(self) -> int:
        return len(self._resolve())

    def __contains__(self, name: object) -> bool:
        return name in self._resolve()

    def __iter__(self):
        return iter(self._resolve().names())

    def __bool__(self) -> bool:
        return bool(self._resolve())

    @property
    def health(self) -> Any:
        return self._resolve().health
