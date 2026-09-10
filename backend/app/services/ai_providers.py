from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.ai_providers import (
    AI_PROVIDER_CREDENTIAL_FIELDS,
    AI_PROVIDER_KINDS,
    AI_PROVIDERS_BY_KIND,
    AiProviderAccount,
)
from app.services import credentials as credential_svc

MASKED_SECRET_VALUE = "•••••"
PROBE_TIMEOUT_SECONDS = 10.0

PLATFORM_KEY_ATTRS: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
    "groq": "groq_api_key",
    "google": "google_api_key",
    "deepgram": "deepgram_api_key",
    "assemblyai": "assemblyai_api_key",
    "elevenlabs": "elevenlabs_api_key",
    "cartesia": "cartesia_api_key",
}

PLATFORM_KEY_ENV_NAMES: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "deepseek": "deepseek-chat",
    "groq": "llama-3.3-70b-versatile",
    "google": "gemini-1.5-flash",
}

DEFAULT_STT_MODELS: dict[str, str] = {
    "deepgram": "nova-2",
    "assemblyai": "best",
}

DEFAULT_TTS_MODELS: dict[str, str] = {
    "elevenlabs": "eleven_turbo_v2_5",
    "cartesia": "sonic-english",
}


def kind_for(provider: str) -> str | None:
    for kind, providers in AI_PROVIDERS_BY_KIND.items():
        if provider in providers:
            return kind
    return None


def validate_kind_and_provider(kind: str, provider: str) -> None:
    if kind not in AI_PROVIDER_KINDS or provider not in AI_PROVIDERS_BY_KIND.get(kind, ()):
        raise ValidationFailedError(f"We do not support a provider called '{provider}' yet.")


def validate_credentials(provider: str, data: dict, *, partial: bool = False) -> dict:
    if provider not in AI_PROVIDER_CREDENTIAL_FIELDS:
        raise ValidationFailedError(f"We do not support a provider called '{provider}' yet.")

    fields = AI_PROVIDER_CREDENTIAL_FIELDS[provider]
    unknown = set(data) - set(fields)
    if unknown:
        raise ValidationFailedError(f"Unknown setting for {provider}: {sorted(unknown)[0]}")

    cleaned: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailedError(f"{key} must be a non-empty string")
        if fields[key] and value == MASKED_SECRET_VALUE:
            if partial:
                continue
            raise ValidationFailedError(
                f"{key} looks like a masked placeholder, not a real key"
            )
        cleaned[key] = value

    if not partial:
        required = [k for k, is_secret in fields.items() if is_secret]
        missing = [k for k in required if k not in cleaned]
        if missing:
            raise ValidationFailedError(
                f"Missing required settings for {provider}: {sorted(missing)}"
            )

    return cleaned


def mask(provider: str, data: dict) -> dict[str, str]:
    fields = AI_PROVIDER_CREDENTIAL_FIELDS[provider]
    out: dict[str, str] = {}
    for field_name, is_secret in fields.items():
        value = data.get(field_name) or ""
        if is_secret:
            out[field_name] = MASKED_SECRET_VALUE if value else ""
        else:
            out[field_name] = value or ""
    return out


def field_flags(provider: str) -> dict[str, bool]:
    return dict(AI_PROVIDER_CREDENTIAL_FIELDS[provider])


def describe_kind(kind: str) -> str:
    return {
        "llm": "language model",
        "stt": "speech recognition",
        "tts": "voice",
    }.get(kind, kind)


async def list_accounts(session: AsyncSession, org_id: uuid.UUID) -> list[AiProviderAccount]:
    return list(
        (
            await session.execute(
                sa.select(AiProviderAccount)
                .where(AiProviderAccount.org_id == org_id)
                .order_by(
                    AiProviderAccount.kind,
                    AiProviderAccount.provider,
                    AiProviderAccount.label,
                )
            )
        )
        .scalars()
        .all()
    )


async def get_account(
    session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID
) -> AiProviderAccount:
    account = (
        await session.execute(
            sa.select(AiProviderAccount).where(
                AiProviderAccount.org_id == org_id, AiProviderAccount.id == account_id
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise NotFoundError("We could not find that connection.")
    return account


async def create_account(
    session: AsyncSession,
    settings: Settings,
    *,
    org_id: uuid.UUID,
    kind: str,
    provider: str,
    label: str,
    credentials: dict,
    actor_user_id: uuid.UUID | None,
) -> AiProviderAccount:
    validate_kind_and_provider(kind, provider)
    cleaned = validate_credentials(provider, credentials)
    encrypted = credential_svc.encrypt(settings, cleaned)
    label = label.strip() if label else ""

    existing = (
        await session.execute(
            sa.select(AiProviderAccount).where(
                AiProviderAccount.org_id == org_id,
                AiProviderAccount.kind == kind,
                AiProviderAccount.provider == provider,
                AiProviderAccount.label == label,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status != "disabled":
            raise ConflictError(
                "A connection for that provider already exists. Disable it first, "
                "or update it instead."
            )
        existing.credentials_encrypted = encrypted
        existing.label = label
        existing.status = "unverified"
        existing.last_probe_at = None
        existing.last_probe_detail = None
        existing.created_by = actor_user_id
        return existing

    account = AiProviderAccount(
        id=uuid.uuid4(),
        org_id=org_id,
        kind=kind,
        provider=provider,
        label=label,
        credentials_encrypted=encrypted,
        status="unverified",
        created_by=actor_user_id,
    )
    session.add(account)
    return account


async def update_account(
    session: AsyncSession,
    settings: Settings,
    account: AiProviderAccount,
    *,
    label: str | None = None,
    credentials: dict | None = None,
    status: str | None = None,
) -> AiProviderAccount:
    if label is not None:
        account.label = label.strip()

    if credentials is not None:
        cleaned = validate_credentials(account.provider, credentials, partial=True)
        stored = credential_svc.decrypt(settings, account.credentials_encrypted)
        merged = {**stored, **cleaned}
        account.credentials_encrypted = credential_svc.encrypt(settings, merged)
        account.status = "unverified"
        account.last_probe_at = None
        account.last_probe_detail = None

    if status is not None:
        if status not in ("disabled", "unverified"):
            raise ValidationFailedError("Status must be disabled or unverified.")
        account.status = status

    return account


async def delete_account(session: AsyncSession, account: AiProviderAccount) -> None:
    await session.delete(account)


async def active_account_for(
    session: AsyncSession, org_id: uuid.UUID, kind: str, provider: str = ""
) -> AiProviderAccount | None:
    stmt = (
        sa.select(AiProviderAccount)
        .where(
            AiProviderAccount.org_id == org_id,
            AiProviderAccount.kind == kind,
            AiProviderAccount.status == "active",
        )
    )
    if provider:
        stmt = stmt.where(AiProviderAccount.provider == provider)
    stmt = stmt.order_by(AiProviderAccount.created_at, AiProviderAccount.id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def byok_completeness(session: AsyncSession, org_id: uuid.UUID) -> tuple[bool, list[str]]:
    missing_kinds: list[str] = []
    for kind in AI_PROVIDER_KINDS:
        if await active_account_for(session, org_id, kind) is None:
            missing_kinds.append(kind)
    return (not missing_kinds), missing_kinds


def platform_key_for(settings: Settings, provider: str) -> str:
    attr = PLATFORM_KEY_ATTRS.get(provider)
    if not attr:
        return ""
    value = getattr(settings, attr, "")
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value or "")


def _secret_field_name(provider: str) -> str:
    for name, is_secret in AI_PROVIDER_CREDENTIAL_FIELDS.get(provider, {}).items():
        if is_secret:
            return name
    return "api_key"


async def resolve_call_config(
    session: AsyncSession,
    settings: Settings,
    *,
    org: Any,
    profile: Any,
    include_keys: bool,
) -> dict:
    mode = org.ai_key_mode

    # In byok mode the ACCOUNT and the PROVIDER must agree. A profile naming a provider
    # explicitly (say llm_provider="anthropic") while the org's only active llm account is
    # OpenAI must NOT silently hand the OpenAI key to Anthropic - that would be an
    # authentication failure at best and a key leaked to the wrong vendor at worst. So the
    # account is resolved for the (kind, provider) PAIR whenever the profile names one, and
    # the kind counts as missing when no active account matches it.
    byok_accounts: dict[str, AiProviderAccount | None] = dict.fromkeys(AI_PROVIDER_KINDS, None)
    providers: dict[str, str] = {}

    for kind in AI_PROVIDER_KINDS:
        explicit = (getattr(profile, f"{kind}_provider", "") or "").strip()
        if mode == "byok":
            account = await active_account_for(session, org.id, kind, provider=explicit or "")
            byok_accounts[kind] = account
            providers[kind] = explicit or (account.provider if account is not None else "")
        else:
            provider = explicit
            if not provider:
                for candidate in AI_PROVIDERS_BY_KIND.get(kind, ()):
                    if platform_key_for(settings, candidate):
                        provider = candidate
                        break
            providers[kind] = provider

    llm_provider = providers["llm"]
    stt_provider = providers["stt"]
    tts_provider = providers["tts"]

    llm_model = (getattr(profile, "llm_model", "") or "") or DEFAULT_MODELS.get(
        llm_provider, ""
    )
    stt_model = DEFAULT_STT_MODELS.get(stt_provider, "")
    tts_model = DEFAULT_TTS_MODELS.get(tts_provider, "")

    tts_voice_id = getattr(profile, "voice_id", "") or ""
    if not tts_voice_id:
        if mode == "byok" and byok_accounts.get("tts") is not None:
            tts_creds = credential_svc.decrypt(
                settings, byok_accounts["tts"].credentials_encrypted
            )
            tts_voice_id = tts_creds.get("default_voice_id", "") or ""
        elif mode == "platform" and tts_provider == "elevenlabs":
            tts_voice_id = settings.elevenlabs_voice_id or ""

    missing: list[str] = []
    if mode == "byok":
        missing = [
            describe_kind(kind)
            for kind in AI_PROVIDER_KINDS
            if byok_accounts.get(kind) is None
        ]
    else:
        # ONE actionable env-var name per kind, never the whole catalogue: when no
        # provider resolved for a kind, name the key for that kind's default provider
        # (the first in AI_PROVIDERS_BY_KIND) - that is the one an operator should set.
        for kind in AI_PROVIDER_KINDS:
            provider = providers[kind] or (AI_PROVIDERS_BY_KIND.get(kind, ("",))[0])
            env_name = PLATFORM_KEY_ENV_NAMES.get(provider)
            if env_name and not platform_key_for(settings, provider):
                missing.append(env_name)

    keys: dict[str, str] | None = None
    if include_keys:
        keys = {}
        if mode == "byok":
            for kind in AI_PROVIDER_KINDS:
                account = byok_accounts.get(kind)
                if account is None:
                    keys[kind] = ""
                else:
                    decrypted = credential_svc.decrypt(
                        settings, account.credentials_encrypted
                    )
                    keys[kind] = decrypted.get(_secret_field_name(account.provider), "") or ""
        else:
            keys = {
                "llm": platform_key_for(settings, llm_provider) if llm_provider else "",
                "stt": platform_key_for(settings, stt_provider) if stt_provider else "",
                "tts": platform_key_for(settings, tts_provider) if tts_provider else "",
            }

    return {
        "mode": mode,
        "llm": {"provider": llm_provider, "model": llm_model},
        "stt": {"provider": stt_provider, "model": stt_model},
        "tts": {"provider": tts_provider, "model": tts_model, "voice_id": tts_voice_id},
        "keys": keys,
        "missing": missing,
    }


@dataclass(frozen=True)
class AiProbeResult:
    provider: str
    ok: bool
    detail: str
    checked: str = ""


_PROBE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "deepseek": "https://api.deepseek.com/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "deepgram": "https://api.deepgram.com/v1/projects",
    "assemblyai": "https://api.assemblyai.com/v2/transcript?limit=1",
    "elevenlabs": "https://api.elevenlabs.io/v1/voices",
    "cartesia": "https://api.cartesia.ai/voices",
}


def _probe_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "openai":
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "deepseek":
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "groq":
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "google":
        return {"x-goog-api-key": api_key}
    if provider == "deepgram":
        return {"Authorization": f"Token {api_key}"}
    if provider == "assemblyai":
        return {"Authorization": api_key}
    if provider == "elevenlabs":
        return {"xi-api-key": api_key}
    if provider == "cartesia":
        return {"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"}
    return {}


def _detail_from(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or f"HTTP {resp.status_code}")[:255]
    if isinstance(body, dict):
        for key in ("message", "detail", "err_msg"):
            value = body.get(key)
            if value:
                return str(value)[:255]
        error = body.get("error")
        if error is not None:
            if isinstance(error, str) and error:
                return error[:255]
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:255]
            if isinstance(error, dict) and error:
                return str(error)[:255]
    return f"HTTP {resp.status_code}"


async def probe(
    provider: str, credentials: dict, *, client: httpx.AsyncClient | None = None
) -> AiProbeResult:
    url = _PROBE_URLS.get(provider)
    if url is None:
        return AiProbeResult(
            provider, False, "We do not know how to check that provider yet.", ""
        )

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)
    try:
        api_key = credentials.get("api_key", "") or ""
        resp = await http.get(url, headers=_probe_headers(provider, api_key))
        if resp.status_code == 200:
            return AiProbeResult(provider, True, "Connection works.", url)
        return AiProbeResult(provider, False, _detail_from(resp), url)
    except httpx.TransportError as exc:
        return AiProbeResult(
            provider, False, f"Could not reach {provider}: {exc}"[:255], url
        )
    except Exception as exc:  # noqa: BLE001 - a probe never propagates
        return AiProbeResult(
            provider, False, f"That check did not finish: {exc}"[:255], url
        )
    finally:
        if owns_client:
            await http.aclose()


async def probe_account(
    session: AsyncSession,
    settings: Settings,
    account: AiProviderAccount,
    *,
    client: httpx.AsyncClient | None = None,
) -> AiProviderAccount:
    credentials = credential_svc.decrypt(settings, account.credentials_encrypted)
    result = await probe(account.provider, credentials, client=client)
    account.last_probe_at = datetime.now(timezone.utc)
    # B1 (Opus P23a verify): providers echo the key in auth errors ("Incorrect API key
    # provided: sk-..."). Scrub every credential value before the detail is stored in a
    # plaintext column that settings:read can list.
    detail = result.detail or ""
    for value in (credentials or {}).values():
        if isinstance(value, str) and len(value) >= 6 and value in detail:
            detail = detail.replace(value, "[redacted]")
    account.last_probe_detail = detail[:512]
    account.status = "active" if result.ok else "failed"
    return account
