"""Tests for per-workspace feature entitlements."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import FeatureDisabledError
from app.models import Org
from app.models.entitlements import OrgFeature
from app.services import entitlements


async def _new_org(session, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    org = Org(
        id=org_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{org_id.hex[:6]}",
    )
    session.add(org)
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def test_defaults(session):
    org_id = await _new_org(session, "Defaults")
    values = await entitlements.for_org(session, org_id)

    assert set(values) == set(entitlements.CATALOG)
    assert values["voice"] is True
    assert values["call_recording"] is False


async def test_set_feature_upsert(session):
    org_id = await _new_org(session, "Upsert")

    await entitlements.set_feature(
        session,
        org_id,
        "voice",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )
    assert await entitlements.has(session, org_id, "voice") is False

    await entitlements.set_feature(
        session,
        org_id,
        "voice",
        enabled=True,
        price_override_micros=None,
        actor_user_id=None,
    )
    assert await entitlements.has(session, org_id, "voice") is True

    await session.flush()
    rows = (
        (
            await session.execute(
                sa.select(OrgFeature)
                .where(OrgFeature.org_id == org_id)
                .execution_options(allow_unscoped=True)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].feature_key == "voice"
    assert rows[0].enabled is True


async def test_require_raises_feature_disabled(session):
    org_id = await _new_org(session, "Require")

    await entitlements.set_feature(
        session,
        org_id,
        "voice",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )
    with pytest.raises(FeatureDisabledError) as exc_info:
        await entitlements.require(session, org_id, "voice")
    assert exc_info.value.code == "feature_disabled"

    await entitlements.set_feature(
        session,
        org_id,
        "voice",
        enabled=True,
        price_override_micros=None,
        actor_user_id=None,
    )
    await entitlements.require(session, org_id, "voice")


async def test_unknown_key_raises_valueerror(session):
    org_id = await _new_org(session, "Unknown")

    with pytest.raises(ValueError, match="unknown feature: nope"):
        await entitlements.has(session, org_id, "nope")
    with pytest.raises(ValueError, match="unknown feature: nope"):
        await entitlements.set_feature(
            session,
            org_id,
            "nope",
            enabled=False,
            price_override_micros=None,
            actor_user_id=None,
        )


async def test_isolation_between_orgs(session):
    a = await _new_org(session, "A Telecom")
    b = await _new_org(session, "B Plumbing")

    assert await entitlements.has(session, a, "voice") is True
    assert await entitlements.has(session, b, "voice") is True

    await entitlements.set_feature(
        session,
        a,
        "voice",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )

    assert await entitlements.has(session, a, "voice") is False
    assert await entitlements.has(session, b, "voice") is True


async def test_memo_invalidation(session):
    org_id = await _new_org(session, "Memo")

    assert await entitlements.has(session, org_id, "voice") is True
    await entitlements.set_feature(
        session,
        org_id,
        "voice",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )
    assert await entitlements.has(session, org_id, "voice") is False


async def test_reads_unscoped_while_context_is_other_org(session):
    a = await _new_org(session, "Unscoped A")
    b = await _new_org(session, "Unscoped B")

    await entitlements.set_feature(
        session,
        a,
        "voice",
        enabled=False,
        price_override_micros=None,
        actor_user_id=None,
    )
    await session.commit()
    entitlements.invalidate(session, a)

    set_org_context(session, b)
    assert await entitlements.has(session, a, "voice") is False
    assert await entitlements.has(session, b, "voice") is True

    values = await entitlements.for_org(session, a)
    assert values["voice"] is False
    assert values["sms"] is True
