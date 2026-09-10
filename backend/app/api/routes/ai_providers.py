from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_settings, require_permission
from app.config import Settings
from app.errors import CarrierNotConfiguredError, ValidationFailedError
from app.models.ai_providers import AI_KEY_MODES, AI_PROVIDER_CREDENTIAL_FIELDS
from app.services import ai_providers as ai_providers_svc
from app.services import audit as audit_svc
from app.services import credentials as credential_svc

router = APIRouter(prefix="/api/v1/ai", tags=["ai-providers"])


class AiProviderCreateIn(BaseModel):
    kind: str
    provider: str
    label: str = Field(default="", max_length=127)
    credentials: dict[str, str]


class AiProviderPatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=127)
    credentials: dict[str, str] | None = None
    status: str | None = None


class AiProviderOut(BaseModel):
    id: uuid.UUID
    kind: str
    provider: str
    label: str
    status: str
    last_probe_at: datetime | None
    last_probe_detail: str | None
    #: The binding P23a contract for POST /providers/{id}/probe is {status, detail}, and the
    #: frontend reads `.detail`. It is the same string as last_probe_detail - carried under
    #: both names so the probe response satisfies the contract without giving that one
    #: endpoint a different body shape from list/create/patch.
    detail: str | None
    fields: dict[str, bool]
    credentials: dict[str, str]
    has_credentials: bool


class AiSettingsIn(BaseModel):
    ai_key_mode: str


class AiSettingsOut(BaseModel):
    ai_key_mode: str
    byok_ready: bool
    missing_kinds: list[str]
    missing_kind_labels: list[str]


def _require_master_key(settings: Settings) -> None:
    if not credential_svc.master_key_present(settings):
        raise CarrierNotConfiguredError("credential storage not configured")


def _non_secret_field_names(provider: str, data: dict | None = None) -> list[str]:
    public = [
        k for k, is_secret in AI_PROVIDER_CREDENTIAL_FIELDS[provider].items() if not is_secret
    ]
    if data is None:
        return public
    return [k for k in data if k in public]


def _account_out(account, settings: Settings) -> AiProviderOut:
    decrypted = credential_svc.decrypt(settings, account.credentials_encrypted)
    required_secrets = [
        k
        for k, is_secret in AI_PROVIDER_CREDENTIAL_FIELDS[account.provider].items()
        if is_secret
    ]
    return AiProviderOut(
        id=account.id,
        kind=account.kind,
        provider=account.provider,
        label=account.label,
        status=account.status,
        last_probe_at=account.last_probe_at,
        last_probe_detail=account.last_probe_detail,
        detail=account.last_probe_detail,
        fields=ai_providers_svc.field_flags(account.provider),
        credentials=ai_providers_svc.mask(account.provider, decrypted),
        has_credentials=all(decrypted.get(k) for k in required_secrets),
    )


async def _settings_out(ctx: OrgContext) -> AiSettingsOut:
    byok_ready, missing_kinds = await ai_providers_svc.byok_completeness(
        ctx.session, ctx.org.id
    )
    return AiSettingsOut(
        ai_key_mode=ctx.org.ai_key_mode,
        byok_ready=byok_ready,
        missing_kinds=missing_kinds,
        missing_kind_labels=[ai_providers_svc.describe_kind(k) for k in missing_kinds],
    )


@router.get("/providers", response_model=list[AiProviderOut])
async def list_ai_providers(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[AiProviderOut]:
    _require_master_key(settings)
    accounts = await ai_providers_svc.list_accounts(ctx.session, ctx.org.id)
    return [_account_out(account, settings) for account in accounts]


@router.post("/providers", response_model=AiProviderOut, status_code=status.HTTP_201_CREATED)
async def create_ai_provider(
    payload: AiProviderCreateIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AiProviderOut:
    _require_master_key(settings)
    account = await ai_providers_svc.create_account(
        ctx.session,
        settings,
        org_id=ctx.org.id,
        kind=payload.kind,
        provider=payload.provider,
        label=payload.label,
        credentials=payload.credentials,
        actor_user_id=ctx.actor_user_id,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="ai_providers.create",
        target_type="ai_provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "kind": account.kind,
            "provider": account.provider,
            "fields": _non_secret_field_names(account.provider, payload.credentials),
        },
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)


@router.get("/settings", response_model=AiSettingsOut)
async def get_ai_settings(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> AiSettingsOut:
    return await _settings_out(ctx)


@router.patch("/settings", response_model=AiSettingsOut)
async def patch_ai_settings(
    payload: AiSettingsIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> AiSettingsOut:
    if payload.ai_key_mode not in AI_KEY_MODES:
        raise ValidationFailedError(
            "We only support the platform or byok mode for AI keys."
        )
    ctx.org.ai_key_mode = payload.ai_key_mode

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="ai.settings.update",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"ai_key_mode": payload.ai_key_mode},
    )
    await ctx.session.commit()
    return await _settings_out(ctx)


@router.patch("/providers/{account_id}", response_model=AiProviderOut)
async def patch_ai_provider(
    account_id: uuid.UUID,
    payload: AiProviderPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AiProviderOut:
    _require_master_key(settings)
    account = await ai_providers_svc.get_account(ctx.session, ctx.org.id, account_id)

    updated_fields: list[str] = []
    if payload.label is not None:
        updated_fields.append("label")
    if payload.credentials is not None:
        updated_fields.extend(
            _non_secret_field_names(account.provider, payload.credentials)
        )
    if payload.status is not None:
        updated_fields.append("status")

    await ai_providers_svc.update_account(
        ctx.session,
        settings,
        account,
        label=payload.label,
        credentials=payload.credentials,
        status=payload.status,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="ai_providers.update",
        target_type="ai_provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "kind": account.kind,
            "provider": account.provider,
            "fields": updated_fields,
        },
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)


@router.delete("/providers/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_provider(
    account_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _require_master_key(settings)
    account = await ai_providers_svc.get_account(ctx.session, ctx.org.id, account_id)
    # Read what the audit row needs BEFORE the delete - after it, the instance is
    # pending-deleted and attribute access can trigger a refresh on a row that is gone.
    deleted = {"id": str(account.id), "kind": account.kind, "provider": account.provider}
    await ai_providers_svc.delete_account(ctx.session, account)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="ai_providers.delete",
        target_type="ai_provider_account",
        target_id=deleted["id"],
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "kind": deleted["kind"],
            "provider": deleted["provider"],
            "fields": _non_secret_field_names(deleted["provider"]),
        },
    )
    await ctx.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/providers/{account_id}/probe", response_model=AiProviderOut)
async def probe_ai_provider(
    account_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AiProviderOut:
    _require_master_key(settings)
    account = await ai_providers_svc.get_account(ctx.session, ctx.org.id, account_id)
    await ai_providers_svc.probe_account(ctx.session, settings, account)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="ai_providers.probe",
        target_type="ai_provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "kind": account.kind,
            "provider": account.provider,
            "fields": _non_secret_field_names(account.provider),
        },
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)
