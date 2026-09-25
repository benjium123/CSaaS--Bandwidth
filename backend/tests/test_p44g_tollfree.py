"""P44g: toll-free inbound traffic-pumping guard."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import set_org_context
from app.models import Call, Org, OrgNumber
from app.services import tollfree_guard

TF = "+18885550100"
LOCAL = "+12145550100"


async def _org(session) -> Org:
    org = Org(id=uuid.uuid4(), name="TF Org", slug=f"tf-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    session.add(OrgNumber(id=uuid.uuid4(), org_id=org.id, e164=TF, carrier="telnyx",
                          number_type="tollfree"))
    session.add(OrgNumber(id=uuid.uuid4(), org_id=org.id, e164=LOCAL, carrier="telnyx",
                          number_type="local"))
    await session.commit()
    return org


def _call(org_id, *, to=TF, caller="+12145559999", status="ringing",
          ago=timedelta(seconds=5), seconds=0) -> Call:
    return Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", our_e164=to,
        contact_e164=caller, carrier="telnyx", status=status,
        created_at=datetime.now(timezone.utc) - ago, duration_seconds=seconds,
    )


async def _new(session, org, **kw) -> Call:
    call = _call(org.id, **kw)
    session.add(call)
    await session.commit()
    return call


@pytest.mark.parametrize(
    ("caller", "bad"),
    [("+12145559999", False), ("+14165551234", False), ("", True), ("anonymous", True),
     ("+447911123456", True), ("+18765551234", True)],
)
def test_bad_caller(caller, bad):
    assert tollfree_guard.bad_caller(caller) is bad


async def test_local_numbers_are_not_guarded(session, settings):
    org = await _org(session)
    call = await _new(session, org, to=LOCAL, caller="")
    assert await tollfree_guard.refusal(session, settings, call) is None


async def test_anonymous_caller_to_toll_free_is_refused(session, settings):
    org = await _org(session)
    call = await _new(session, org, caller="")
    assert await tollfree_guard.refusal(session, settings, call) == "tf_bad_caller"


async def test_concurrency_cap(session, settings):
    org = await _org(session)
    for i in range(5):
        session.add(_call(org.id, caller=f"+1214555{1000 + i}", status="in_progress"))
    await session.commit()
    call = await _new(session, org, caller="+12145558888")
    assert await tollfree_guard.refusal(session, settings, call) == "tf_concurrency"


async def test_same_caller_rate(session, settings):
    org = await _org(session)
    for m in (1, 2, 3):
        session.add(_call(org.id, status="completed", ago=timedelta(minutes=m)))
    await session.commit()
    call = await _new(session, org)
    assert await tollfree_guard.refusal(session, settings, call) == "tf_caller_rate"


async def test_daily_minutes_cap(session, settings):
    org = await _org(session)
    session.add(_call(org.id, caller="+12145550001", status="completed",
                      ago=timedelta(minutes=30), seconds=500 * 60))
    await session.commit()
    call = await _new(session, org, caller="+12145550002")
    assert await tollfree_guard.refusal(session, settings, call) == "tf_daily_cap"


async def test_normal_call_passes(session, settings):
    org = await _org(session)
    call = await _new(session, org)
    assert await tollfree_guard.refusal(session, settings, call) is None
