from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_settings, require_permission
from app.config import Settings
from app.errors import CarrierNotConfiguredError
from app.models.provider_accounts import PROVIDER_CREDENTIAL_FIELDS
from app.services import audit as audit_svc
from app.services import credentials as credential_svc
from app.services import provider_accounts as provider_accounts_svc

router = APIRouter(prefix="/api/v1/provider-accounts", tags=["provider-accounts"])


class ProviderAccountCreateIn(BaseModel):
    provider: str
    label: str = Field(default="", max_length=127)
    credentials: dict[str, str]


class ProviderAccountPatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=127)
    credentials: dict[str, str] | None = None


class ProviderAccountOut(BaseModel):
    id: uuid.UUID
    provider: str
    label: str
    status: str
    last_probe_at: datetime | None
    last_probe_detail: str | None
    credentials: dict[str, str]


def _require_master_key(settings: Settings) -> None:
    if not credential_svc.master_key_present(settings):
        raise CarrierNotConfiguredError("credential storage not configured")


def _account_out(account, settings: Settings) -> ProviderAccountOut:
    decrypted = credential_svc.decrypt(settings, account.credentials_encrypted)
    masked = provider_accounts_svc.mask(account.provider, decrypted)
    return ProviderAccountOut(
        id=account.id,
        provider=account.provider,
        label=account.label,
        status=account.status,
        last_probe_at=account.last_probe_at,
        last_probe_detail=account.last_probe_detail,
        credentials=masked,
    )


def _non_secret_field_names(provider: str, data: dict | None = None) -> list[str]:
    public = [
        k for k, is_secret in PROVIDER_CREDENTIAL_FIELDS[provider].items() if not is_secret
    ]
    if data is None:
        return public
    return [k for k in data if k in public]


@router.get("", response_model=list[ProviderAccountOut])
async def list_provider_accounts(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ProviderAccountOut]:
    _require_master_key(settings)
    accounts = await provider_accounts_svc.list_accounts(ctx.session)
    return [_account_out(account, settings) for account in accounts]


@router.post("", response_model=ProviderAccountOut, status_code=status.HTTP_201_CREATED)
async def create_provider_account(
    payload: ProviderAccountCreateIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProviderAccountOut:
    _require_master_key(settings)
    account = await provider_accounts_svc.create_account(
        ctx.session,
        settings,
        org_id=ctx.org.id,
        provider=payload.provider,
        label=payload.label,
        credentials=payload.credentials,
        actor_user_id=ctx.actor_user_id,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="provider_accounts.create",
        target_type="provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "provider": account.provider,
            "fields": _non_secret_field_names(account.provider),
        },
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)


@router.patch("/{account_id}", response_model=ProviderAccountOut)
async def patch_provider_account(
    account_id: uuid.UUID,
    payload: ProviderAccountPatchIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProviderAccountOut:
    _require_master_key(settings)
    account = await provider_accounts_svc.get_account(ctx.session, account_id)

    updated_fields: list[str] = []
    if payload.label is not None:
        updated_fields.append("label")
    if payload.credentials is not None:
        updated_fields.extend(_non_secret_field_names(account.provider, payload.credentials))

    await provider_accounts_svc.update_account(
        ctx.session,
        settings,
        account,
        label=payload.label,
        credentials=payload.credentials,
    )

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="provider_accounts.update",
        target_type="provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={"provider": account.provider, "fields": updated_fields},
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)


@router.post("/{account_id}/probe", response_model=ProviderAccountOut)
async def probe_provider_account(
    account_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProviderAccountOut:
    _require_master_key(settings)
    account = await provider_accounts_svc.get_account(ctx.session, account_id)
    await provider_accounts_svc.probe_account(ctx.session, settings, account)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="provider_accounts.probe",
        target_type="provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "provider": account.provider,
            "fields": _non_secret_field_names(account.provider),
        },
    )
    await ctx.session.commit()
    await ctx.session.refresh(account)
    return _account_out(account, settings)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_provider_account(
    account_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _require_master_key(settings)
    account = await provider_accounts_svc.get_account(ctx.session, account_id)
    await provider_accounts_svc.disable_account(ctx.session, account)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="provider_accounts.disable",
        target_type="provider_account",
        target_id=str(account.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        detail={
            "provider": account.provider,
            "fields": _non_secret_field_names(account.provider),
        },
    )
    await ctx.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
