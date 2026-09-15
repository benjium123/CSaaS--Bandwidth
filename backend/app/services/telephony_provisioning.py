"""P37a: provision one Telnyx MANAGED SUB-ACCOUNT per org, with zero portal clicks.

The whole point of this module is what it ENDS with: a perfectly ordinary per-org
``provider_accounts`` row for provider ``telnyx``, holding the SUB-ACCOUNT's API key and
messaging profile id. Every existing path - sending through the per-org carrier registry
(``providers/registry_org.py``), inbound webhooks, number ordering - then works unchanged,
because each already resolves an org's Telnyx credentials from exactly that row
(``services/provider_accounts.py::settings_like_for``). There is deliberately NO second
send/receive path for managed orgs.

``telephony_accounts`` is the resumable scratchpad in front of that: one row per org,
recording each Telnyx object as it is created so a re-run picks up where the last one
stopped and NEVER creates a second managed account or messaging profile.

Secrets: the sub-account API key is encrypted with ``services/credentials`` before it
touches the DB, and neither it nor the platform master key is ever logged, audited,
returned by a route, or written into ``last_error``.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.numbers import persist_ordered_number, to_e164
from app.config import Settings
from app.db.base import set_org_context
from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models import Org, OrgNumber, ProviderAccount
from app.models.telephony import TelephonyAccount
from app.providers.telnyx.adapter import DEFAULT_BASE_URL, TelnyxMessagingCarrier
from app.services import credentials as credential_svc
from app.services import provider_accounts as provider_accounts_svc
from app.services import telephony_billing

log = structlog.get_logger("telephony_provisioning")

#: The ordered steps of ``provision``. A re-run skips every one whose result is already
#: recorded on the row, which is what makes the whole thing idempotent AND resumable.
STEPS: tuple[str, ...] = ("managed_account", "api_key", "messaging_profile", "provider_account")

#: Verbatim sentence the contract requires when Telnyx hands back a managed account with
#: no API key on it - the single most likely way this fails on a real first run, and the
#: operator needs to be told precisely what to ask Telnyx for.
MISSING_API_KEY_MESSAGE = (
    "Telnyx did not return an API key for the managed account - confirm Managed "
    "Accounts API access with Telnyx"
)

_OK_STATUSES = (200, 201, 202)


def _base_url(settings: Settings) -> str:
    # config.py has no telnyx_base_url field today and P37a may not add one (allowed
    # files: the two managed-telephony fields only), so this reads one if a deployment
    # ever grows it and otherwise uses the adapter's own default.
    return str(getattr(settings, "telnyx_base_url", "") or DEFAULT_BASE_URL).rstrip("/")


def _master_key(settings: Settings) -> str:
    return settings.telnyx_master_api_key.get_secret_value().strip()


def require_managed_telephony(settings: Settings) -> None:
    """503 unless managed telephony is both switched on AND actually configured.

    Two distinct 503s on purpose: "not enabled" is a deliberate operator choice, "no
    master key" is a half-finished deployment, and an operator staring at a dark feature
    needs to know which of the two they are looking at.
    """
    if not settings.telephony_managed_enabled:
        raise FeatureUnavailableError("Managed telephony is not enabled")
    if not _master_key(settings):
        raise FeatureUnavailableError(
            "Managed telephony is not configured - the platform master API key is unset"
        )


class TelnyxManagedClient:
    """The few Telnyx calls P37a makes, over an INJECTABLE httpx client.

    Injectable because every test in tests/test_p37a_provisioning.py drives it through
    ``httpx.MockTransport``: the alternative - monkeypatching module globals - would let
    the request shapes (URL, method, which key signs which call) drift untested, and
    "which key signed it" is precisely the thing a sub-account bug hides behind.
    """

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL, http: httpx.AsyncClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._owns_client = http is None

    async def http(self) -> httpx.AsyncClient:
        """The live client, created on first use. Public because ONE provisioning run
        must make every call - including the provider-account probe in step 4 - through
        the same client: two clients means the injected test transport covers only half
        the run, and the half it misses is the half that would hit the real internet."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def aclose(self) -> None:
        if self._owns_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _call(
        self, method: str, path: str, api_key: str, *, json: dict | None = None
    ) -> dict:
        client = await self.http()
        try:
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TransportError as exc:
            # The exception text can carry a URL but never a credential; still, only a
            # fixed sentence reaches the caller - last_error is shown to the customer.
            raise _StepFailed(f"Could not reach Telnyx ({type(exc).__name__})") from exc
        if resp.status_code not in _OK_STATUSES:
            raise _StepFailed(f"Telnyx refused the request with HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise _StepFailed("Telnyx returned a response we could not read") from exc
        data = (payload or {}).get("data")
        return data if isinstance(data, dict) else {}

    async def create_managed_account(self, master_key: str, business_name: str) -> dict:
        return await self._call(
            "POST", "/managed_accounts", master_key, json={"business_name": business_name}
        )

    async def get_managed_account(self, master_key: str, managed_account_id: str) -> dict:
        return await self._call("GET", f"/managed_accounts/{managed_account_id}", master_key)

    async def create_messaging_profile(
        self, api_key: str, *, name: str, webhook_url: str, failover_url: str
    ) -> dict:
        return await self._call(
            "POST",
            "/messaging_profiles",
            api_key,
            json={
                "name": name,
                "webhook_url": webhook_url,
                "webhook_failover_url": failover_url,
                "webhook_api_version": "2",
                "whitelisted_destinations": ["US", "CA"],
            },
        )


class _StepFailed(Exception):
    """One provisioning step could not complete. Carries the PLAIN-ENGLISH sentence that
    lands in ``telephony_accounts.last_error`` and is shown to the customer - never raw
    Telnyx JSON, never a status body, and never anything derived from a credential."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _webhook_urls(settings: Settings) -> tuple[str, str]:
    base = (settings.public_base_url or "").rstrip("/")
    url = f"{base}/api/v1/webhooks/telnyx/messaging"
    return url, url


async def get_account(session: AsyncSession, org_id: uuid.UUID) -> TelephonyAccount | None:
    set_org_context(session, org_id)
    return (
        await session.execute(sa.select(TelephonyAccount).where(TelephonyAccount.org_id == org_id))
    ).scalar_one_or_none()


def steps_done(account: TelephonyAccount | None) -> list[dict[str, Any]]:
    """Per-step completion, derived from what is actually STORED rather than from
    ``last_step`` - a step is done when its result exists, which is the same condition
    ``provision`` itself skips on, so the UI can never disagree with the engine."""
    done = {
        "managed_account": bool(account and account.managed_account_id),
        "api_key": bool(account and account.api_key_encrypted),
        "messaging_profile": bool(account and account.messaging_profile_id),
        "provider_account": bool(account and account.status == "active"),
    }
    return [{"key": key, "done": done[key]} for key in STEPS]


async def _fail(
    session: AsyncSession, account: TelephonyAccount, message: str
) -> None:
    account.status = "failed"
    account.last_error = message[:512]
    await session.commit()


async def provision(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    *,
    http: httpx.AsyncClient | None = None,
) -> TelephonyAccount:
    """Bring this org's managed Telnyx sub-account all the way to a usable state.

    Idempotent and resumable: each step commits its own result before the next one runs,
    and every step is skipped when its result is already on the row. Calling this twice
    on a healthy org makes ZERO Telnyx create calls; calling it after a failure resumes
    at the step that failed rather than starting a second managed account.
    """
    require_managed_telephony(settings)
    set_org_context(session, org_id)

    org = await session.get(Org, org_id)
    if org is None:
        raise ValidationFailedError("Unknown workspace")

    account = await get_account(session, org_id)
    if account is None:
        account = TelephonyAccount(
            id=uuid.uuid4(), org_id=org_id, provider="telnyx", status="provisioning"
        )
        session.add(account)
        await session.commit()

    if account.status == "suspended":
        # Fable review: without this, POST /setup was a self-service un-suspend - a
        # suspended row went "provisioning", every step was skipped (all results already
        # stored) and it came out "active". Suspension (P37c) is lifted by ops only.
        raise FeatureUnavailableError(
            "This workspace's texting and calling are suspended - contact support"
        )

    if account.status != "active":
        account.status = "provisioning"
        account.last_error = None
        await session.commit()

    client = TelnyxManagedClient(base_url=_base_url(settings), http=http)
    try:
        await _run_steps(session, settings, org, account, client)
    except _StepFailed as exc:
        await _fail(session, account, exc.message)
        log.warning(
            "telephony_provisioning.failed",
            org_id=str(org_id),
            step=account.last_step,
            reason=exc.message,
        )
        raise FeatureUnavailableError(exc.message) from exc
    except Exception:
        await _fail(session, account, "Provisioning could not be completed - please retry")
        log.exception("telephony_provisioning.unexpected", org_id=str(org_id))
        raise
    finally:
        if http is None:
            await client.aclose()

    return account


async def _run_steps(
    session: AsyncSession,
    settings: Settings,
    org: Org,
    account: TelephonyAccount,
    client: TelnyxManagedClient,
) -> str:
    master_key = _master_key(settings)

    # ---- 1. the managed sub-account -------------------------------------------------
    if not account.managed_account_id:
        data = await client.create_managed_account(master_key, org.name or str(org.id))
        managed_id = str(data.get("id") or "").strip()
        if not managed_id:
            raise _StepFailed("Telnyx created the account but did not return its id")
        account.managed_account_id = managed_id[:64]
        account.last_step = "managed_account"
        await session.commit()

    # ---- 2. its API key -------------------------------------------------------------
    if not account.api_key_encrypted:
        data = await client.get_managed_account(master_key, account.managed_account_id or "")
        raw_key = str(data.get("api_key") or "").strip()
        if not raw_key:
            # UNVERIFIED at contract time whether this endpoint carries api_key at all.
            # Stopping here (rather than guessing another endpoint) leaves the managed
            # account id already recorded, so the retry after Telnyx enables the access
            # resumes HERE and never creates a second sub-account.
            raise _StepFailed(MISSING_API_KEY_MESSAGE)
        account.api_key_encrypted = credential_svc.encrypt(settings, {"api_key": raw_key})
        account.last_step = "api_key"
        await session.commit()
        api_key = raw_key
    else:
        api_key = str(
            credential_svc.decrypt(settings, account.api_key_encrypted).get("api_key", "")
        )
        if not api_key:
            raise _StepFailed(MISSING_API_KEY_MESSAGE)

    # ---- 3. the messaging profile, webhooks already pointed at us -------------------
    if not account.messaging_profile_id:
        webhook_url, failover_url = _webhook_urls(settings)
        data = await client.create_messaging_profile(
            api_key,
            name=f"{org.name or org.id} (managed)",
            webhook_url=webhook_url,
            failover_url=failover_url,
        )
        profile_id = str(data.get("id") or "").strip()
        if not profile_id:
            raise _StepFailed("Telnyx created the messaging profile but did not return its id")
        account.messaging_profile_id = profile_id[:64]
        account.last_step = "messaging_profile"
        await session.commit()

    # ---- 4. the provider_accounts row every existing path already reads -------------
    await _upsert_provider_account(
        session, settings, org.id, account, api_key, http=await client.http()
    )
    account.status = "active"
    account.last_step = "provider_account"
    account.last_error = None
    await session.commit()
    # Bumped AFTER the commit, exactly like api/routes/provider_accounts.py does: a bump
    # before it lets a concurrent request prime a per-org carrier registry from a row
    # that is not yet durable.
    provider_accounts_svc.bump_version(org.id)
    return api_key


async def _upsert_provider_account(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    account: TelephonyAccount,
    api_key: str,
    *,
    http: httpx.AsyncClient | None,
) -> ProviderAccount:
    set_org_context(session, org_id)
    creds: dict[str, str] = {
        "api_key": api_key,
        "messaging_profile_id": account.messaging_profile_id or "",
    }
    # UNVERIFIED: which Ed25519 public key signs a SUB-ACCOUNT's webhooks. Telnyx exposes
    # no documented per-managed-account public-key endpoint, so the platform's configured
    # key is stored as the best available answer. Webhook verification itself is NOT
    # weakened anywhere - if this key turns out to be wrong for sub-accounts, inbound
    # webhooks fail closed (rejected), which is the safe direction and is loud.
    platform_public_key = settings.telnyx_public_key.get_secret_value().strip()
    if platform_public_key:
        creds["public_key"] = platform_public_key

    existing = (
        await session.execute(
            sa.select(ProviderAccount).where(
                ProviderAccount.org_id == org_id, ProviderAccount.provider == "telnyx"
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        row = await provider_accounts_svc.create_account(
            session,
            settings,
            org_id=org_id,
            provider="telnyx",
            label="Managed Telnyx account",
            credentials=creds,
            actor_user_id=None,
        )
    else:
        # create_account() refuses a non-disabled duplicate (ConflictError) - a re-run
        # after a partial failure MUST update the row it already made, not collide
        # with it.
        row = await provider_accounts_svc.update_account(
            session, settings, existing, credentials=creds
        )
    await session.commit()

    # Only ACTIVE once the credentials have actually been accepted by Telnyx. Marking a
    # row active on faith is how an org ends up "provisioned" and unable to send.
    await provider_accounts_svc.probe_account(session, settings, row, client=http)
    await session.commit()
    if row.status != "active":
        raise _StepFailed(
            "Telnyx did not accept the new sub-account's credentials - "
            "provisioning will retry from this step"
        )
    return row


async def order_number(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    e164: str,
    *,
    http: httpx.AsyncClient | None = None,
) -> OrgNumber:
    """Buy one number INSIDE this org's managed sub-account, on its messaging profile.

    The prepaid gate runs exactly as it does in api/routes/numbers.py::order - refuse
    before the carrier is ever called, charge the first month after the row is stored -
    because a managed org is still a prepaid org and a second, laxer purchase path would
    be a hole straight through the gate.
    """
    require_managed_telephony(settings)
    set_org_context(session, org_id)

    account = await get_account(session, org_id)
    if account is None or account.status != "active" or not account.api_key_encrypted:
        raise FeatureUnavailableError(
            "This workspace's telephony account is not ready yet - finish setup first"
        )
    api_key = str(
        credential_svc.decrypt(settings, account.api_key_encrypted).get("api_key", "")
    )
    if not api_key:
        raise FeatureUnavailableError(
            "This workspace's telephony account is not ready yet - finish setup first"
        )

    normalized = to_e164(e164)
    # Pre-check before the carrier is touched, same as the route: an already-registered
    # number must never reach Telnyx and become an orphaned purchase.
    existing = (
        await session.execute(sa.select(OrgNumber.id).where(OrgNumber.e164 == normalized))
    ).first()
    if existing is not None:
        raise ConflictError(f"{normalized} is already registered")

    await telephony_billing.require_number_credit(session, org_id, "telnyx")

    carrier = TelnyxMessagingCarrier(
        api_key=api_key,
        messaging_profile_id=account.messaging_profile_id or "",
        public_key=settings.telnyx_public_key.get_secret_value(),
        base_url=_base_url(settings),
        client=http,
    )
    try:
        result = await carrier.order_number(normalized)
    finally:
        if http is None:
            await carrier.aclose()

    provider_account_id = (
        await session.execute(
            sa.select(ProviderAccount.id).where(
                ProviderAccount.org_id == org_id,
                ProviderAccount.provider == "telnyx",
                ProviderAccount.status == "active",
            )
        )
    ).scalar_one_or_none()

    number = await persist_ordered_number(
        session,
        org_id,
        carrier,
        result,
        provider_account_id=provider_account_id,
        provisioning={"messaging_profile_id": account.messaging_profile_id or ""},
    )
    await telephony_billing.charge_new_number(session, org_id, number)
    await session.commit()
    return number
