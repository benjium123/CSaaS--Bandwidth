"""Port-out PIN (services/port_pin.py) and the operator port-out decisions
(services/porting.py + api/routes/porting.py).

Service tests copy the FakeTelnyx / _settings / _verified_org pattern from
tests/test_p44f_porting.py; route tests use the conftest client with a
credentials_master_key installed via a module-local `settings` override (the
reveal/rotate routes encrypt the PIN, and conftest's plain make_settings()
leaves credentials_master_key blank).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, ValidationFailedError
from app.models import (
    AuditLogEntry,
    Org,
    OrgMembership,
    OrgNumber,
    PortRequest,
    Role,
    SecurityAlert,
    User,
)
from app.services import port_pin, porting
from tests.conftest import (
    auth_headers,
    create_org,
    make_settings,
    mark_recent_2fa,
    register_and_login,
)
from tests.test_p44f_porting import FakeTelnyx, _settings, _verified_org

OWNER_EMAIL = "pin-owner@example.com"


# ----------------------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------------------


@pytest.fixture
def settings():
    """The route tests reach the app through conftest's `client`, which is built from this
    fixture. The reveal/rotate routes encrypt the PIN, so the app needs a real credential
    key - conftest's plain make_settings() leaves credentials_master_key blank."""
    return make_settings(credentials_master_key=Fernet.generate_key().decode())


def _recording_handler(calls: list):
    """A Telnyx fake that records (method, path, json body) and answers 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"data": {}})

    return handler


def _number(
    org: Org,
    e164: str,
    *,
    carrier: str = "telnyx",
    status: str = "active",
    is_active: bool = True,
    provider_ref: str | None = None,
) -> OrgNumber:
    return OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164=e164,
        carrier=carrier,
        status=status,
        is_active=is_active,
        provider_ref=provider_ref,
        provisioning={},
    )


async def _numbers(session, org_id: uuid.UUID) -> list[OrgNumber]:
    return list(
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.org_id == org_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def _out_ports(session, org_id: uuid.UUID) -> list[PortRequest]:
    return list(
        (
            await session.execute(
                sa.select(PortRequest)
                .where(PortRequest.direction == "out")
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def _the_out_port(session, org_id: uuid.UUID) -> PortRequest:
    rows = await _out_ports(session, org_id)
    assert len(rows) == 1
    return rows[0]


async def _alerts(session, kind: str) -> list[SecurityAlert]:
    return list(
        (await session.execute(sa.select(SecurityAlert).where(SecurityAlert.kind == kind)))
        .scalars()
        .all()
    )


def _out_port(
    org: Org,
    *,
    carrier_ref: str = "out-1",
    status: str = "pending",
    numbers: list[str] | None = None,
    details: dict | None = None,
) -> PortRequest:
    return PortRequest(
        id=uuid.uuid4(),
        org_id=org.id,
        direction="out",
        carrier="telnyx",
        numbers=numbers or ["+12145550123"],
        status=status,
        carrier_ref=carrier_ref,
        details=details if details is not None else {},
        events=[],
    )


async def _operator_user(session) -> User:
    from app.repositories import users as users_repo

    user = await users_repo.create_user(
        session, email=f"op-{uuid.uuid4().hex[:8]}@example.com", password="x" * 16
    )
    await session.commit()
    return user


def _reminder_events(port: PortRequest) -> list[dict]:
    return [
        event
        for event in (port.events or [])
        if "Reminder sent to operators" in str(event.get("text") or "")
    ]


def _error_codes(response: httpx.Response) -> set[str]:
    """Every ``code`` the error envelope carries, whatever level it sits at."""
    try:
        body = response.json()
    except ValueError:
        return set()
    found: set[str] = set()
    pending = [body]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("code"), str):
                found.add(item["code"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


async def _add_member(
    session, org_id: uuid.UUID, email: str, role_name: str, permissions: list[str]
) -> User:
    """Attach a non-admin member with a custom role (test_delivery_digest's pattern)."""
    set_org_context(session, org_id)
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == email.lower())
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    role = Role(
        id=uuid.uuid4(), org_id=org_id, name=role_name, permissions=permissions, is_system=False
    )
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()
    return user


# ==================================================================================
# port_pin.account_id
# ==================================================================================


def test_account_id_is_readable_and_unique_per_workspace():
    org = Org(id=uuid.uuid4(), name="Sabine Property Group", slug="sabine-property-group")
    value = port_pin.account_id(org)
    assert value.startswith("RL-SABINE-")
    assert re.fullmatch(r"RL-[A-Z0-9]{1,6}-[A-Z2-7]{4}", value)

    twin = Org(id=uuid.uuid4(), name="Sabine Property Group", slug="sabine-property-group")
    assert port_pin.account_id(twin).startswith("RL-SABINE-")
    assert port_pin.account_id(twin) != value


def test_account_id_falls_back_to_org_for_a_nameless_workspace():
    org = Org(id=uuid.uuid4(), name="", slug="")
    assert re.fullmatch(r"RL-ORG-[A-Z2-7]{4}", port_pin.account_id(org))


# ==================================================================================
# port_pin.new_pin
# ==================================================================================


def test_new_pin_skips_repeats_and_sequences(monkeypatch):
    draws = iter([111111, 123456, 482913])
    monkeypatch.setattr(port_pin.secrets, "randbelow", lambda _n: next(draws))
    assert port_pin.new_pin() == "482913"


def test_new_pin_is_six_digits_and_never_a_run():
    for _ in range(25):
        pin = port_pin.new_pin()
        assert re.fullmatch(r"\d{6}", pin)
        assert len(set(pin)) > 1
        assert pin not in ("123456", "654321")


# ==================================================================================
# port_pin.get_or_create
# ==================================================================================


async def test_get_or_create_is_stable_and_never_stores_the_pin_in_clear(session):
    settings = _settings()
    org = await _verified_org(session)
    first = await port_pin.get_or_create(session, settings, org.id)
    await session.commit()
    assert re.fullmatch(r"\d{6}", first)
    assert await port_pin.get_or_create(session, settings, org.id) == first

    row = (
        await session.execute(
            sa.select(port_pin.PlatformSetting)
            .where(port_pin.PlatformSetting.key == f"port_pin:{org.id}")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert "enc" in row.value and "fp" in row.value
    assert first not in json.dumps(row.value)


# ==================================================================================
# port_pin.apply_org / rotate / back-off / tick
# ==================================================================================


async def test_apply_org_pins_active_telnyx_numbers_only(session):
    settings = _settings()
    org = await _verified_org(session)
    session.add_all(
        [
            _number(org, "+12145550101", provider_ref="tx-1"),
            _number(org, "+12145550102", provider_ref="tx-2"),
            _number(org, "+12145550103", carrier="signalwire"),
            _number(
                org, "+12145550104", status="released", is_active=False, provider_ref="tx-9"
            ),
        ]
    )
    await session.commit()

    calls: list = []
    registry = {"telnyx": FakeTelnyx(_recording_handler(calls))}
    counts = await port_pin.apply_org(session, settings, registry, org.id)
    assert counts == {"applied": 2, "failed": 0}

    pin = await port_pin.get_or_create(session, settings, org.id)
    assert re.fullmatch(r"\d{6}", pin)
    assert len(calls) == 2
    assert {c[1] for c in calls} == {"/v2/phone_numbers/tx-1", "/v2/phone_numbers/tx-2"}
    for method, _path, body in calls:
        assert method == "PATCH"
        assert body == {"external_pin": pin}

    fp = port_pin.fingerprint(org.id, pin)
    numbers = await _numbers(session, org.id)
    by_ref = {n.provider_ref: n for n in numbers if n.provider_ref}
    assert by_ref["tx-1"].provisioning["port_pin_fp"] == fp
    assert by_ref["tx-2"].provisioning["port_pin_fp"] == fp
    untouched = [n for n in numbers if n.carrier != "telnyx"]
    assert len(untouched) == 1
    assert "port_pin_fp" not in (untouched[0].provisioning or {})

    calls.clear()
    assert await port_pin.apply_org(session, settings, registry, org.id) == {
        "applied": 0,
        "failed": 0,
    }
    assert calls == []

    view = await port_pin.status(session, org.id)
    assert view["has_pin"] is True
    assert view["numbers_total"] == 2
    assert view["numbers_protected"] == 2


async def test_rotate_puts_a_new_pin_on_every_number(session):
    settings = _settings()
    org = await _verified_org(session)
    session.add_all(
        [
            _number(org, "+12145550101", provider_ref="tx-1"),
            _number(org, "+12145550102", provider_ref="tx-2"),
        ]
    )
    await session.commit()

    old = await port_pin.get_or_create(session, settings, org.id)
    await session.commit()
    calls: list = []
    registry = {"telnyx": FakeTelnyx(_recording_handler(calls))}
    assert await port_pin.apply_org(session, settings, registry, org.id) == {
        "applied": 2,
        "failed": 0,
    }

    calls.clear()
    new = await port_pin.rotate(session, settings, org.id, None)
    await session.commit()
    assert new != old
    assert await port_pin.apply_org(session, settings, registry, org.id) == {
        "applied": 2,
        "failed": 0,
    }
    assert len(calls) == 2
    for _method, _path, body in calls:
        assert body == {"external_pin": new}


async def test_apply_org_backs_off_a_refused_number_until_the_pin_changes(session):
    settings = _settings()
    org = await _verified_org(session)
    session.add_all(
        [
            _number(org, "+12145550101", provider_ref="tx-1"),
            _number(org, "+12145550102", provider_ref="tx-2"),
        ]
    )
    await session.commit()

    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/tx-2"):
            return httpx.Response(422, json={"errors": [{"detail": "PIN refused"}]})
        return httpx.Response(200, json={"data": {}})

    registry = {"telnyx": FakeTelnyx(handler)}
    assert await port_pin.apply_org(session, settings, registry, org.id) == {
        "applied": 1,
        "failed": 1,
    }
    by_ref = {n.provider_ref: n for n in await _numbers(session, org.id) if n.provider_ref}
    assert "port_pin_error" in by_ref["tx-2"].provisioning
    assert "port_pin_error" not in by_ref["tx-1"].provisioning

    # The very next sweep must not hammer the number the carrier refused.
    calls.clear()
    assert await port_pin.apply_org(session, settings, registry, org.id) == {
        "applied": 0,
        "failed": 0,
    }
    assert calls == []

    # A new PIN is always tried at once.
    await port_pin.rotate(session, settings, org.id, None)
    await session.commit()
    calls.clear()
    await port_pin.apply_org(session, settings, registry, org.id)
    assert any(path.endswith("/tx-2") for _method, path, _body in calls)


async def test_tick_pins_every_workspace_with_telnyx_numbers(session, engine):
    settings = _settings()
    org_a = Org(id=uuid.uuid4(), name="Tick A", slug=f"tick-a-{uuid.uuid4().hex[:8]}")
    org_b = Org(id=uuid.uuid4(), name="Tick B", slug=f"tick-b-{uuid.uuid4().hex[:8]}")
    session.add_all([org_a, org_b])
    await session.flush()
    set_org_context(session, org_a.id)
    session.add_all(
        [
            _number(org_a, "+12145550101", provider_ref="a-1"),
            _number(org_a, "+12145550102", provider_ref="a-2"),
        ]
    )
    await session.commit()
    set_org_context(session, org_b.id)
    session.add(_number(org_b, "+12145550103", provider_ref="b-1"))
    await session.commit()

    calls: list = []
    registry = {"telnyx": FakeTelnyx(_recording_handler(calls))}
    factory = async_sessionmaker(engine, expire_on_commit=False)
    totals = await port_pin.tick(factory, settings, registry)
    assert totals == {"applied": 3, "failed": 0}
    assert len(calls) == 3


# ==================================================================================
# porting.poll_port_outs
# ==================================================================================


def _port_out_poll_handler(status: str = "pending"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "out-1",
                        "phone_numbers": ["+12145550123"],
                        "status": status,
                        "carrier_name": "Verizon",
                        "requested_foc_date": "2026-06-20",
                    }
                ]
            },
        )

    return handler


async def test_poll_records_a_pending_port_out_once(session):
    settings = _settings()
    org = await _verified_org(session)
    session.add(
        OrgNumber(id=uuid.uuid4(), org_id=org.id, e164="+12145550123", carrier="telnyx")
    )
    await session.commit()

    registry = {"telnyx": FakeTelnyx(_port_out_poll_handler())}
    assert await porting.poll_port_outs(session, settings, registry) == 1

    out = await _the_out_port(session, org.id)
    assert out.status == "pending"
    assert out.details["gaining_carrier"] == "Verizon"
    respond_by = datetime.fromisoformat(out.details["respond_by"])
    remaining = respond_by - datetime.now(timezone.utc)
    assert timedelta(hours=23) < remaining <= timedelta(hours=24, minutes=5)

    assert len(await _alerts(session, "port_out_request")) == 1

    assert await porting.poll_port_outs(session, settings, registry) == 0
    assert len(await _out_ports(session, org.id)) == 1


async def test_poll_reminds_the_operators_once_about_a_stale_request(session):
    settings = _settings()
    org = await _verified_org(session)
    session.add(
        OrgNumber(id=uuid.uuid4(), org_id=org.id, e164="+12145550123", carrier="telnyx")
    )
    await session.commit()

    registry = {"telnyx": FakeTelnyx(_port_out_poll_handler())}
    assert await porting.poll_port_outs(session, settings, registry) == 1

    out = await _the_out_port(session, org.id)
    out.created_at = datetime.now(timezone.utc) - timedelta(hours=13)
    await session.commit()

    await porting.poll_port_outs(session, settings, registry)
    out = await _the_out_port(session, org.id)
    assert out.details.get("reminded") is True
    assert len(_reminder_events(out)) == 1

    await porting.poll_port_outs(session, settings, registry)
    out = await _the_out_port(session, org.id)
    assert len(_reminder_events(out)) == 1


# ==================================================================================
# porting.authorize_port_out
# ==================================================================================


async def test_authorize_port_out_releases_the_numbers_once(session):
    org = await _verified_org(session)
    operator = await _operator_user(session)
    out = _out_port(org)
    session.add(out)
    await session.commit()

    calls: list = []
    registry = {"telnyx": FakeTelnyx(_recording_handler(calls))}
    await porting.authorize_port_out(session, registry, out, operator.id)
    assert calls == [("PATCH", "/v2/portouts/out-1/authorized", {})]

    out = await _the_out_port(session, org.id)
    assert out.status == "authorized"
    assert out.reviewed_by == operator.id

    with pytest.raises(ConflictError):
        await porting.authorize_port_out(session, registry, out, operator.id)


async def test_authorize_port_out_keeps_it_pending_when_the_carrier_refuses(session):
    org = await _verified_org(session)
    operator = await _operator_user(session)
    out = _out_port(org)
    session.add(out)
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"errors": [{"detail": "not allowed"}]})

    registry = {"telnyx": FakeTelnyx(handler)}
    with pytest.raises(ConflictError):
        await porting.authorize_port_out(session, registry, out, operator.id)
    out = await _the_out_port(session, org.id)
    assert out.status == "pending"


# ==================================================================================
# porting.port_out_rejection_codes
# ==================================================================================


async def test_port_out_rejection_codes_always_offer_other(session):
    org = await _verified_org(session)
    out = _out_port(org)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"code": 1002, "reason": "PIN mismatch", "reason_required": False}
                ]
            },
        )

    codes = await porting.port_out_rejection_codes({"telnyx": FakeTelnyx(handler)}, out)
    by_code = {c["code"]: c for c in codes}
    assert set(by_code) == {1002, 1001}
    assert by_code[1002]["label"] == "PIN mismatch"
    assert by_code[1002]["reason_required"] is False
    assert by_code[1001]["label"] == "Other"
    assert by_code[1001]["reason_required"] is True

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    codes = await porting.port_out_rejection_codes({"telnyx": FakeTelnyx(broken)}, out)
    assert [c["code"] for c in codes] == [1001]


# ==================================================================================
# porting.reject_port_out
# ==================================================================================


async def test_reject_port_out_needs_a_reason_for_other_and_patches_the_carrier(session):
    org = await _verified_org(session)
    operator = await _operator_user(session)
    out = _out_port(org)
    session.add(out)
    await session.commit()

    calls: list = []
    registry = {"telnyx": FakeTelnyx(_recording_handler(calls))}
    with pytest.raises(ValidationFailedError):
        await porting.reject_port_out(session, registry, out, operator.id, code=1001, reason="")
    assert calls == []

    await porting.reject_port_out(session, registry, out, operator.id, code=1002)
    assert calls == [("PATCH", "/v2/portouts/out-1/rejected-pending", {"rejection_code": 1002})]
    out = await _the_out_port(session, org.id)
    assert out.status == "rejected"


# ==================================================================================
# porting.dispute_port_out
# ==================================================================================


async def test_dispute_flags_a_pending_port_out_exactly_once(session):
    settings = _settings()
    org = await _verified_org(session)
    out = _out_port(org)
    session.add(out)
    await session.commit()
    set_org_context(session, org.id)
    user_id = uuid.uuid4()

    await porting.dispute_port_out(session, settings, out, user_id)
    out = await _the_out_port(session, org.id)
    assert out.details["disputed_by"] == str(user_id)
    assert len(await _alerts(session, "port_out_disputed")) == 1

    await porting.dispute_port_out(session, settings, out, user_id)
    assert len(await _alerts(session, "port_out_disputed")) == 1


async def test_dispute_refuses_a_request_that_is_not_a_port_out(session):
    settings = _settings()
    org = await _verified_org(session)
    incoming = PortRequest(
        id=uuid.uuid4(),
        org_id=org.id,
        direction="in",
        carrier="telnyx",
        numbers=["+12145550123"],
        status="pending",
        details={},
        events=[],
    )
    session.add(incoming)
    await session.commit()
    with pytest.raises(ConflictError):
        await porting.dispute_port_out(session, settings, incoming, uuid.uuid4())


# ==================================================================================
# PORT_OUT_STATUS_MAP
# ==================================================================================


def test_port_out_status_map_translates_telnyx_states():
    assert porting.PORT_OUT_STATUS_MAP["rejected-pending"] == "rejected"
    assert porting.PORT_OUT_STATUS_MAP["authorized"] == "authorized"


# ==================================================================================
# Routes: the workspace view of the PIN
# ==================================================================================


async def test_owner_ports_list_shows_the_account_id_and_pin_holder(client, session):
    token = await register_and_login(client, OWNER_EMAIL)
    org = await create_org(client, token, "Sabine Property Group")

    response = await client.get("/api/v1/ports", headers=auth_headers(token, org["id"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"].startswith("RL-")
    assert body["pin_holder"] is True


async def test_pin_reveal_needs_fresh_2fa_then_returns_a_stable_pin(client, session):
    token = await register_and_login(client, OWNER_EMAIL)
    org = await create_org(client, token, "Reveal Co")

    blocked = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert blocked.status_code in (401, 403), blocked.text
    assert "step_up_required" in _error_codes(blocked)

    await mark_recent_2fa(session, OWNER_EMAIL)
    first = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert first.status_code == 200, first.text
    pin = first.json()["pin"]
    assert re.fullmatch(r"\d{6}", pin)

    again = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert again.status_code == 200, again.text
    assert again.json()["pin"] == pin

    await session.rollback()
    set_org_context(session, uuid.UUID(org["id"]))
    logged = (
        await session.execute(
            sa.select(AuditLogEntry).where(AuditLogEntry.action == "porting.pin_viewed")
        )
    ).scalars().all()
    assert len(logged) >= 1


async def test_pin_rotate_returns_a_new_pin_that_reveal_then_shows(client, session):
    token = await register_and_login(client, OWNER_EMAIL)
    org = await create_org(client, token, "Rotate Co")
    await mark_recent_2fa(session, OWNER_EMAIL)

    first = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert first.status_code == 200, first.text
    original = first.json()["pin"]

    rotated = await client.post(
        "/api/v1/ports/pin/rotate", headers=auth_headers(token, org["id"])
    )
    assert rotated.status_code == 200, rotated.text
    new_pin = rotated.json()["pin"]
    assert re.fullmatch(r"\d{6}", new_pin)
    assert new_pin != original

    again = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert again.json()["pin"] == new_pin


async def test_a_non_admin_member_never_sees_the_pin(client, session):
    owner_token = await register_and_login(client, OWNER_EMAIL)
    org = await create_org(client, owner_token, "Pin Reader Co")
    org_id = uuid.UUID(org["id"])

    reader_token = await register_and_login(client, "pin-reader@example.com")
    await _add_member(session, org_id, "pin-reader@example.com", "pin_reader", ["numbers:manage"])

    status = await client.get("/api/v1/ports/pin", headers=auth_headers(reader_token, org_id))
    assert status.status_code in (401, 403), status.text
    assert "pin_owner_only" in _error_codes(status)

    reveal = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(reader_token, org_id)
    )
    assert reveal.status_code in (401, 403), reveal.text
    assert "pin_owner_only" in _error_codes(reveal)

    ports = await client.get("/api/v1/ports", headers=auth_headers(reader_token, org_id))
    assert ports.status_code == 200, ports.text
    assert ports.json()["pin_holder"] is False
    assert "pin" not in ports.json()


async def test_the_pin_never_appears_in_the_workspace_responses(client, session):
    token = await register_and_login(client, OWNER_EMAIL)
    org = await create_org(client, token, "No Leak Co")
    await mark_recent_2fa(session, OWNER_EMAIL)

    revealed = await client.post(
        "/api/v1/ports/pin/reveal", headers=auth_headers(token, org["id"])
    )
    assert revealed.status_code == 200, revealed.text
    pin = revealed.json()["pin"]

    ports = await client.get("/api/v1/ports", headers=auth_headers(token, org["id"]))
    assert ports.status_code == 200, ports.text
    assert pin not in ports.text

    status = await client.get("/api/v1/ports/pin", headers=auth_headers(token, org["id"]))
    assert status.status_code == 200, status.text
    assert pin not in status.text
