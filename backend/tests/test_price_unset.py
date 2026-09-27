# ruff: noqa: F811 - pytest fixtures imported from test_ops_console
"""Price-unset alerts (P46 P2b): metering, listing, feature toggles, ops routes, sweep."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import CreditLedgerEntry, PlatformPrice, SecurityAlert
from app.services import credits, discounts, price_alerts, telephony_billing
from tests.conftest import auth_headers
from tests.test_ops_console import _new_org, _operator, ops, ops_settings  # noqa: F401

METRIC = "recording_min"
LIST_PRICE = 50_000


async def _usage_rows(session, org_id: uuid.UUID) -> list[CreditLedgerEntry]:
    """The org's usage ledger rows (mirrors the discount tests' ledger query)."""
    set_org_context(session, org_id)
    return list(
        (
            await session.execute(
                sa.select(CreditLedgerEntry)
                .where(
                    CreditLedgerEntry.org_id == org_id,
                    CreditLedgerEntry.entry_type == "usage",
                )
                .order_by(CreditLedgerEntry.seq)
            )
        ).scalars()
    )


async def _alerts(session) -> list[SecurityAlert]:
    """Every price_unset alert, whatever its status."""
    return list(
        (
            await session.execute(
                sa.select(SecurityAlert).where(SecurityAlert.kind == "price_unset")
            )
        ).scalars()
    )


async def _open_alerts(session, metric: str | None = None) -> list[SecurityAlert]:
    rows = [alert for alert in await _alerts(session) if alert.status == "open"]
    if metric is not None:
        rows = [alert for alert in rows if (alert.detail or {}).get("metric") == metric]
    return rows


async def _funded_org(session, name: str) -> uuid.UUID:
    """An org with telephony credit, so a feature charge lands in the ledger."""
    org_id = await _new_org(session, name, prepaid=True)
    set_org_context(session, org_id)
    await credits.topup(session, org_id, 5_000_000, reference=f"topup-{uuid.uuid4()}")
    await session.commit()
    return org_id


async def test_unset_metric_charges_nothing_and_opens_alert(session):
    org_id = await _new_org(session, "Unset Meter Org")
    set_org_context(session, org_id)

    charged = await telephony_billing.charge_feature_minutes(
        session, org_id, METRIC, 61, reference="rec:x", note="t"
    )
    await session.commit()

    assert charged == 0
    assert await _usage_rows(session, org_id) == []

    opens = await _open_alerts(session, METRIC)
    assert len(opens) == 1
    detail = opens[0].detail
    assert detail["metric"] == METRIC
    assert detail["unpriced_minutes"] == 2
    assert detail["count"] == 1
    assert str(org_id) in detail["orgs"]


async def test_second_unset_charge_updates_the_same_alert(session):
    org_a = await _new_org(session, "Unset Meter A")
    set_org_context(session, org_a)
    await telephony_billing.charge_feature_minutes(
        session, org_a, METRIC, 61, reference="rec:a", note="t"
    )
    await session.commit()

    org_b = await _new_org(session, "Unset Meter B")
    set_org_context(session, org_b)
    await telephony_billing.charge_feature_minutes(
        session, org_b, METRIC, 30, reference="rec:b", note="t"
    )
    await session.commit()

    opens = await _open_alerts(session, METRIC)
    assert len(opens) == 1
    detail = opens[0].detail
    assert detail["count"] == 2
    assert detail["unpriced_minutes"] == 3
    assert str(org_a) in detail["orgs"]
    assert str(org_b) in detail["orgs"]


async def test_priced_charge_writes_one_idempotent_ledger_row(session):
    org_id = await _funded_org(session, "Priced Meter Org")
    session.add(PlatformPrice(metric=METRIC, price_micros=LIST_PRICE))
    await session.commit()
    set_org_context(session, org_id)

    charged = await telephony_billing.charge_feature_minutes(
        session, org_id, METRIC, 61, reference="rec:x", note="t"
    )
    await session.commit()

    assert charged == 100_000
    rows = await _usage_rows(session, org_id)
    assert len(rows) == 1
    assert rows[0].amount_micros == -100_000
    assert rows[0].reference == "rec:x"

    # The reference is the ledger's unique key, so a repeat does not double-charge.
    await telephony_billing.charge_feature_minutes(
        session, org_id, METRIC, 61, reference="rec:x", note="t"
    )
    await session.commit()
    assert len(await _usage_rows(session, org_id)) == 1


async def test_priced_charge_applies_usage_discount(session):
    org_id = await _funded_org(session, "Discount Meter Org")
    session.add(PlatformPrice(metric=METRIC, price_micros=LIST_PRICE))
    await discounts.set_discount(
        session, org_id, "usage", percent_bps=2000, ends_at=None, note=None, actor_user_id=None
    )
    await session.commit()
    set_org_context(session, org_id)

    charged = await telephony_billing.charge_feature_minutes(
        session, org_id, METRIC, 61, reference="rec:d", note="t"
    )
    await session.commit()

    assert charged == 80_000
    rows = await _usage_rows(session, org_id)
    assert len(rows) == 1
    assert rows[0].discount_micros == 20_000


async def test_zero_or_missing_seconds_is_a_noop(session):
    org_id = await _new_org(session, "Zero Meter Org")
    set_org_context(session, org_id)

    # Unset price: the seconds short-circuit runs before the unset check, so no alert.
    assert (
        await telephony_billing.charge_feature_minutes(
            session, org_id, METRIC, 0, reference="rec:z0", note="t"
        )
        == 0
    )
    assert (
        await telephony_billing.charge_feature_minutes(
            session, org_id, METRIC, None, reference="rec:z1", note="t"
        )
        == 0
    )
    await session.commit()
    assert await _open_alerts(session, METRIC) == []

    # Priced: still nothing charged, so no ledger row.
    session.add(PlatformPrice(metric=METRIC, price_micros=LIST_PRICE))
    await session.commit()
    assert (
        await telephony_billing.charge_feature_minutes(
            session, org_id, METRIC, 0, reference="rec:z2", note="t"
        )
        == 0
    )
    await session.commit()
    assert await _usage_rows(session, org_id) == []


async def test_list_unit_price_for_feature_metric(session):
    org_id = await _new_org(session, "List Price Org")
    set_org_context(session, org_id)

    assert (
        await telephony_billing.list_unit_price(session, org_id, "platform", "transcription_min")
        == 0
    )

    session.add(PlatformPrice(metric="transcription_min", price_micros=30_000))
    await session.commit()

    assert (
        await telephony_billing.list_unit_price(session, org_id, "platform", "transcription_min")
        == 30_000
    )


async def test_sweep_raises_one_alert_per_unset_metric(session):
    org_id = await _new_org(session, "Sweep Org")
    set_org_context(session, org_id)

    raised = await price_alerts.sweep(session)
    assert raised >= 1
    opens = await _open_alerts(session, "transcription_min")
    assert len(opens) == 1

    first_count = opens[0].detail["count"]
    await price_alerts.sweep(session)
    opens = await _open_alerts(session, "transcription_min")
    assert len(opens) == 1
    assert opens[0].detail["count"] > first_count


async def test_set_price_route_resolves_the_alert(session, ops, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Resolve Price Org")
    set_org_context(session, org_id)
    await price_alerts.raise_unset(session, METRIC, org_id=org_id, reason="metered", minutes=2)
    await session.commit()
    assert len(await _open_alerts(session, METRIC)) == 1

    r = await ops.put(
        f"/api/v1/ops/console/prices/{METRIC}",
        json={"price_micros": 40_000},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    reviewed = [
        alert
        for alert in await _alerts(session)
        if (alert.detail or {}).get("metric") == METRIC and alert.status == "reviewed"
    ]
    assert len(reviewed) == 1
    assert reviewed[0].review_note == "price set"

    r = await ops.get("/api/v1/ops/console/prices", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    by_metric = {row["metric"]: row for row in r.json()["prices"]}
    assert by_metric[METRIC]["price_micros"] == 40_000
    assert by_metric[METRIC]["unset"] is False


async def test_get_prices_marks_unset_metrics(session, ops, ops_settings):
    token = await _operator(ops, session)
    await _new_org(session, "List Unset Org")

    r = await ops.get("/api/v1/ops/console/prices", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    by_metric = {row["metric"]: row for row in r.json()["prices"]}

    for metric in ("recording_min", "transcription_min"):
        assert by_metric[metric]["price_micros"] is None
        assert by_metric[metric]["unset"] is True
    assert by_metric["sms_out"]["unset"] is False


async def test_enable_call_recording_blocked_until_priced(session, ops, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Blocked Recording Org")

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/call_recording",
        json={"enabled": True},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 409, r.text
    assert "price_unset" in r.text

    session.add(PlatformPrice(metric=METRIC, price_micros=LIST_PRICE))
    await session.commit()

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/call_recording",
        json={"enabled": True},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 200, r.text


async def test_enable_voicemail_transcription_opens_alert(session, ops, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Voicemail Toggle Org")

    # voicemail_transcription is default ON, so switch it off first.
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/voicemail_transcription",
        json={"enabled": False},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 200, r.text

    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/voicemail_transcription",
        json={"enabled": True},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    opens = await _open_alerts(session, "transcription_min")
    assert len(opens) == 1
    assert "feature_enabled" in opens[0].detail["reasons"]


async def test_org_price_override_charges_while_platform_price_unset(session):
    from app.services import entitlements

    org_id = await _funded_org(session, "Override Org")
    await entitlements.set_feature(
        session, org_id, "call_recording", enabled=True, price_override_micros=30_000,
        actor_user_id=None,
    )
    await session.commit()

    charged = await telephony_billing.charge_feature_minutes(
        session, org_id, METRIC, 61, reference="rec:ovr", note="t", feature="call_recording"
    )
    await session.commit()

    assert charged == 60_000
    rows = await _usage_rows(session, org_id)
    assert len(rows) == 1 and rows[0].amount_micros == -60_000
    assert await _open_alerts(session, METRIC) == []


async def test_enable_with_price_override_is_allowed_while_unset(session, ops, ops_settings):
    token = await _operator(ops, session)
    org_id = await _new_org(session, "Override Enable Org")
    r = await ops.put(
        f"/api/v1/ops/console/orgs/{org_id}/features/call_recording",
        json={"enabled": True, "price_override_micros": 30_000},
        headers={**auth_headers(token), "X-Ops-Reason": "price%20unset%20test"},
    )
    assert r.status_code == 200, r.text
