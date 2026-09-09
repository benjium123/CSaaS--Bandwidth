from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_settings, require_permission
from app.config import Settings
from app.errors import CarrierNotConfiguredError
from app.models import OrgNumber, ProviderSpendDaily
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
    #: OrgNumber rows with provider_account_id == account.id and status != "released".
    numbers_count: int = 0
    #: Sum of provider_spend_daily.cost_micros for this account's provider from the 1st
    #: of the current UTC month through today inclusive. There is no
    #: provider_account_id on provider_spend_daily, so spend is attributed by provider;
    #: that is exact because an org may have only one account per provider.
    spend_mtd_micros: int = 0


def _require_master_key(settings: Settings) -> None:
    if not credential_svc.master_key_present(settings):
        raise CarrierNotConfiguredError("credential storage not configured")


def _account_out(
    account,
    settings: Settings,
    *,
    numbers_count: int = 0,
    spend_mtd_micros: int = 0,
) -> ProviderAccountOut:
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
        numbers_count=numbers_count,
        spend_mtd_micros=spend_mtd_micros,
    )


def _non_secret_field_names(provider: str, data: dict | None = None) -> list[str]:
    public = [
        k for k, is_secret in PROVIDER_CREDENTIAL_FIELDS[provider].items() if not is_secret
    ]
    if data is None:
        return public
    return [k for k in data if k in public]


async def _number_counts(session, account_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(
                OrgNumber.provider_account_id,
                sa.func.count().label("count"),
            )
            .where(
                OrgNumber.provider_account_id.in_(account_ids),
                OrgNumber.status != "released",
            )
            .group_by(OrgNumber.provider_account_id)
        )
    ).all()
    return {row.provider_account_id: int(row.count) for row in rows}


async def _spend_mtd_by_provider(session, providers: set[str]) -> dict[str, int]:
    if not providers:
        return {}
    today = datetime.now(timezone.utc).date()
    start = date(today.year, today.month, 1)
    rows = (
        await session.execute(
            sa.select(
                ProviderSpendDaily.provider,
                sa.func.coalesce(sa.func.sum(ProviderSpendDaily.cost_micros), 0).label(
                    "total"
                ),
            )
            .where(
                ProviderSpendDaily.provider.in_(providers),
                ProviderSpendDaily.period_date >= start,
                ProviderSpendDaily.period_date <= today,
            )
            .group_by(ProviderSpendDaily.provider)
        )
    ).all()
    return {row.provider: int(row.total) for row in rows}


async def _stats_for(
    session, account_ids: set[uuid.UUID], providers: set[str]
) -> tuple[dict[uuid.UUID, int], dict[str, int]]:
    # Exactly two aggregate queries for the list endpoint, regardless of page size.
    return await _number_counts(session, account_ids), await _spend_mtd_by_provider(
        session, providers
    )


async def _account_stats(session, account) -> tuple[int, int]:
    """Scalar stats for one account; used by single-account response routes."""
    # Count a mapped column so the tenant guard scopes this aggregate to the org.
    numbers_count = (
        await session.execute(
            sa.select(sa.func.count(OrgNumber.id)).where(
                OrgNumber.provider_account_id == account.id,
                OrgNumber.status != "released",
            )
        )
    ).scalar_one()

    today = datetime.now(timezone.utc).date()
    start = date(today.year, today.month, 1)
    spend_mtd_micros = int(
        (
            await session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(ProviderSpendDaily.cost_micros), 0)
                ).where(
                    ProviderSpendDaily.provider == account.provider,
                    ProviderSpendDaily.period_date >= start,
                    ProviderSpendDaily.period_date <= today,
                )
            )
        ).scalar_one()
    )
    return int(numbers_count), spend_mtd_micros


@router.get("", response_model=list[ProviderAccountOut])
async def list_provider_accounts(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ProviderAccountOut]:
    _require_master_key(settings)
    accounts = await provider_accounts_svc.list_accounts(ctx.session)
    counts, spends = await _stats_for(
        ctx.session,
        {account.id for account in accounts},
        {account.provider for account in accounts},
    )
    return [
        _account_out(
            account,
            settings,
            numbers_count=counts.get(account.id, 0),
            spend_mtd_micros=spends.get(account.provider, 0),
        )
        for account in accounts
    ]


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
    # 4.11: bump the org's provider-registry version only AFTER the row is durably
    # committed - bumping before commit let a concurrent request observe the new
    # version and prime/cache a registry off a row that might still roll back.
    provider_accounts_svc.bump_version(ctx.org.id)
    await ctx.session.refresh(account)
    numbers_count, spend_mtd_micros = await _account_stats(ctx.session, account)
    return _account_out(
        account,
        settings,
        numbers_count=numbers_count,
        spend_mtd_micros=spend_mtd_micros,
    )


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
    # 4.11: bump the org's provider-registry version only AFTER the row is durably
    # committed - bumping before commit let a concurrent request observe the new
    # version and prime/cache a registry off a row that might still roll back.
    provider_accounts_svc.bump_version(ctx.org.id)
    await ctx.session.refresh(account)
    numbers_count, spend_mtd_micros = await _account_stats(ctx.session, account)
    return _account_out(
        account,
        settings,
        numbers_count=numbers_count,
        spend_mtd_micros=spend_mtd_micros,
    )


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
    # 4.11: bump the org's provider-registry version only AFTER the row is durably
    # committed - bumping before commit let a concurrent request observe the new
    # version and prime/cache a registry off a row that might still roll back.
    provider_accounts_svc.bump_version(ctx.org.id)
    await ctx.session.refresh(account)
    numbers_count, spend_mtd_micros = await _account_stats(ctx.session, account)
    return _account_out(
        account,
        settings,
        numbers_count=numbers_count,
        spend_mtd_micros=spend_mtd_micros,
    )


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
    provider_accounts_svc.bump_version(ctx.org.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
