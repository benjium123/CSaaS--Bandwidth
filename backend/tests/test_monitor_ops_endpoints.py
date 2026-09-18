"""The operator endpoints over HTTP, because the services passing is not the same as the
routes working.

Everything in test_monitor_operator_control.py calls services directly. That is how a key
collision survived: `case_file["recommendation"]` was already taken by the AI case file, where
it holds a plain STRING, so `recommended_level()` called `.get()` on a string and every
decision request against a paused account would have been a 500. No service-level test could
see it, because no service-level test had an AI case file in the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, OrgMonitoring, User
from app.models import Session as IdentitySession
from app.services import monitor_score
from tests.conftest import auth_headers, make_settings, register_and_login


@pytest.fixture
def ops_settings():
    return make_settings(monitor_enforced=True, monitor_auto_action=False, kyc_enforced=False)


@pytest.fixture
async def ops(engine, ops_settings):
    from app.main import create_app

    application = create_app(ops_settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _operator(client, session, email="ops@example.com", role="admin") -> str:
    """A named platform operator with a second factor and a session that proved it."""
    from app.services import operators as operators_svc

    token = await register_and_login(client, email)
    user = (
        await session.execute(
            sa.select(User).where(sa.func.lower(User.email) == email).execution_options(
                allow_unscoped=True
            )
        )
    ).scalar_one()
    user.has_passkey = True
    await operators_svc.grant(session, email=email, role=role)
    live = (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.user_id == user.id)
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    live.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    return token


async def _customer(session, settings, name: str, *, signals: int = 0) -> uuid.UUID:
    org_id = uuid.uuid4()
    slug = f"{name.lower().replace(chr(32), chr(45))}-{org_id.hex[:6]}"
    session.add(Org(id=org_id, name=name, slug=slug))
    await session.commit()
    set_org_context(session, org_id)
    for n in range(signals):
        await monitor_score.add_signal(
            session, settings, org_id, "text_blocked", f"blocked {n}", weight=40
        )
    await session.commit()
    return org_id


async def test_accounts_lists_every_customer_not_just_the_noisy_ones(ops, session, ops_settings):
    token = await _operator(ops, session)
    quiet = await _customer(session, ops_settings, "Quiet Co")
    noisy = await _customer(session, ops_settings, "Noisy Co", signals=3)

    r = await ops.get("/api/v1/ops/monitoring/accounts", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {a["org_id"] for a in body["accounts"]}
    assert str(quiet) in ids and str(noisy) in ids
    assert body["auto_action"] is False, "operators must be able to see the policy in force"

    flagged = next(a for a in body["accounts"] if a["org_id"] == str(noisy))
    assert flagged["needs_decision"] is True
    assert flagged["recommendation"]["level"] == "paused"
    assert flagged["level"] == "watch", "detection must not have restricted the account"
    calm = next(a for a in body["accounts"] if a["org_id"] == str(quiet))
    assert calm["needs_decision"] is False


async def test_needs_decision_is_filtered_before_paging_not_after(ops, session, ops_settings):
    """The bug this pins: filtering a JSON field in Python AFTER limit/offset returns only the
    waiting accounts that happen to land on the first page. With one flagged account buried
    behind twelve quiet ones and a page size of 5, the naive version returns nothing."""
    token = await _operator(ops, session)
    for n in range(12):
        await _customer(session, ops_settings, f"Quiet {n:02d}")
    buried = await _customer(session, ops_settings, "Zebra Last", signals=3)

    r = await ops.get(
        "/api/v1/ops/monitoring/accounts?needs_decision=true&limit=5",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["org_id"] for a in body["accounts"]] == [str(buried)]
    assert body["total"] == 1


async def test_review_on_demand_reports_and_does_not_act(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Review Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/review",
        json={"days": 7, "thorough": True},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["actions_taken"] == []
    assert report["monitor"]["level"] == "watch"
    assert report["monitor"]["recommendation"]["level"] == "paused"
    assert report["window_days"] == 7
    assert "headline" in report

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level == "watch", "an on-demand review must never restrict an account"
    assert state.reviewed_at is not None, "the review should be recorded against the account"


async def test_applying_a_recommendation_pauses_and_arms_the_case_file(ops, session, ops_settings):
    """The second bug this pins: an operator-applied pause must leave the SAME state an
    automatic one does. `case_file["status"] == "pending"` is what makes the sweeper write the
    evidence pack and email the owners - without it we pause a customer and tell nobody."""
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Apply Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "apply", "note": "Confirmed courier impersonation across 4 numbers."},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied_level"] == "paused"

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level == "paused"
    assert state.paused_at is not None
    assert state.paused_reason
    assert (state.case_file or {}).get("status") == "pending", "the case file was never armed"
    assert monitor_score.recommended_level(state) is None, "the recommendation must be spent"


async def test_a_decision_on_a_paused_account_with_an_ai_case_file_does_not_500(
    ops, session, ops_settings
):
    """The key collision, end to end. `case_file["recommendation"]` is a STRING written by the
    AI case file. Reading a pending operator decision out of that same key made `.get()` crash
    on a str, so this request returned 500 instead of a clean 409."""
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Cased Co")
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=True)
    state.level = "paused"
    state.case_file = {
        "status": "written",
        "recommendation": "keep_paused",  # the AI's own key, a plain string
        "evidence": ["three blocked texts"],
    }
    await session.commit()

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "apply", "note": "trying to decide on an account with a case file"},
        headers=auth_headers(token),
    )
    assert r.status_code == 409, f"expected a clean conflict, got {r.status_code}: {r.text}"
    assert "recommendation" in r.text.lower()


async def test_rejecting_clears_the_recommendation_over_http(ops, session, ops_settings):
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Reject Me", signals=3)

    r = await ops.post(
        f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
        json={"action": "reject", "note": "Checked the messages - this is their real business."},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "reject"

    set_org_context(session, org_id)
    state = (
        await session.execute(sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id))
    ).scalar_one()
    await session.refresh(state)
    assert state.level != "paused"
    assert monitor_score.recommended_level(state) is None
    assert state.score == 0, "a rejected recommendation must not be re-raised by the arithmetic"


async def test_the_endpoints_require_an_operator(ops, session, ops_settings):
    """A customer must not be able to read the whole platform's account list."""
    org_id = await _customer(session, ops_settings, "Nosy Co")
    token = await register_and_login(ops, "customer@example.com")
    for method, path, body in (
        ("get", "/api/v1/ops/monitoring/accounts", None),
        ("post", f"/api/v1/ops/monitoring/orgs/{org_id}/review", {"days": 7}),
        (
            "post",
            f"/api/v1/ops/monitoring/orgs/{org_id}/decision",
            {"action": "apply", "note": "no"},
        ),
    ):
        r = await getattr(ops, method)(
            path, headers=auth_headers(token), **({"json": body} if body else {})
        )
        assert r.status_code == 403, f"{path} allowed a non-operator: {r.status_code}"


# ======================================================================================
# Entering a customer's account by hand: read-only inspection.
# ======================================================================================
async def _traffic(session, org_id, *, at):
    """A handful of real messages, including one whose body contains LIKE metacharacters."""
    from app.models import Message, MessageThread

    set_org_context(session, org_id)
    bodies = [
        ("Hi, your plumbing appointment is confirmed for 9am today. Dan.", "+12145557001"),
        ("Hi, your plumbing appointment is confirmed for 2pm today. Dan.", "+12145557002"),
        ("DHL: parcel 90001 held pending a 2.99 fee. Pay: http://dhl-1.top/p", "+19725558001"),
        ("DHL: parcel 90002 held pending a 2.99 fee. Pay: http://dhl-2.top/p", "+13055558002"),
        ("Spring sale: 50% off_now on all callouts this month.", "+12145557003"),
    ]
    for body, to in bodies:
        thread = MessageThread(
            id=uuid.uuid4(), org_id=org_id, our_e164="+12145550100", contact_e164=to,
            last_message_at=at,
        )
        session.add(thread)
        await session.flush()
        session.add(
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
                status="delivered", from_e164="+12145550100", to_e164=to, body=body,
                media=[], created_at=at,
            )
        )
    await session.commit()


async def test_an_operator_can_read_real_traffic_not_just_what_was_flagged(
    ops, session, ops_settings
):
    """The capability the operator asked for: reviewing an account themselves.

    The case file at `GET /orgs/{id}` shows held and blocked messages only - and the premise of
    this whole design is that a blended scammer's messages mostly are NOT held. An operator who
    can only see flagged traffic can only ever agree with the machine.
    """
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Inspect Me")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))

    r = await ops.get(
        f"/api/v1/ops/monitoring/orgs/{org_id}/inspect", headers=auth_headers(token)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_only"] is True
    assert body["traffic"]["outbound_texts"] == 5
    fingerprints = {c["fingerprint"] for c in body["campaigns"]}
    assert len(fingerprints) >= 2, body["campaigns"]
    # The templates are shown without a verdict attached: the operator reads and decides.
    assert all("verdict" not in c for c in body["campaigns"])
    assert any("DHL" in s for c in body["campaigns"] for s in c["samples"])

    r = await ops.get(
        f"/api/v1/ops/monitoring/orgs/{org_id}/messages?direction=outbound",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    log = r.json()
    assert log["total"] == 5
    assert log["read_only"] is True
    # Including the ones nothing ever flagged - which is the point.
    assert {m["moderation_state"] for m in log["messages"]} != {"held"}


async def test_a_body_search_does_not_treat_the_text_as_a_wildcard(ops, session, ops_settings):
    """`50% off_now` contains both LIKE metacharacters. Unescaped, `%` matches anything and `_`
    matches any single character, so this search would return messages that do not contain the
    phrase - while the operator, deciding whether to pause a business, believes they are looking
    at every message that does."""
    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Wildcard Co")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))

    r = await ops.get(
        f"/api/v1/ops/monitoring/orgs/{org_id}/messages?contains=50%25 off_now",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    found = r.json()
    assert found["total"] == 1, [m["body"] for m in found["messages"]]
    assert "50% off_now" in found["messages"][0]["body"]

    # And a pattern that would match everything if the wildcards were live matches nothing.
    r = await ops.get(
        f"/api/v1/ops/monitoring/orgs/{org_id}/messages?contains=%25%25",
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


async def test_reading_customer_messages_is_audited_every_time(ops, session, ops_settings):
    """Not sampled. The audit row is the customer's only protection against an operator reading
    their private messages out of curiosity, and a sampled audit protects nobody."""
    from app.models.platform import AuditLogEntry

    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Audited Co")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))

    for _ in range(2):
        assert (
            await ops.get(
                f"/api/v1/ops/monitoring/orgs/{org_id}/messages", headers=auth_headers(token)
            )
        ).status_code == 200
    assert (
        await ops.get(
            f"/api/v1/ops/monitoring/orgs/{org_id}/inspect", headers=auth_headers(token)
        )
    ).status_code == 200

    set_org_context(session, org_id)
    rows = (
        (
            await session.execute(
                sa.select(AuditLogEntry).where(
                    AuditLogEntry.org_id == org_id,
                    AuditLogEntry.action.like("monitor.account_inspected%"),
                )
            )
        )
        .scalars()
        .all()
    )
    actions = sorted(r.action for r in rows)
    assert actions == [
        "monitor.account_inspected.messages",
        "monitor.account_inspected.messages",
        "monitor.account_inspected.overview",
    ], actions
    # And the row says how much was exposed, not merely that something was read.
    assert all((r.detail or {}).get("bodies_shown") is not None for r in rows)


async def test_inspection_needs_a_recent_second_factor(ops, session, ops_settings):
    """Reading every message a business has sent is the same class of power as pausing it, so it
    sits behind the same step-up. A stolen operator cookie alone must not open the message log."""
    token = await _operator(ops, session)
    user = (
        await session.execute(
            sa.select(User)
            .where(sa.func.lower(User.email) == "ops@example.com")
            .execution_options(allow_unscoped=True)
        )
    ).scalar_one()
    live = (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.user_id == user.id)
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    live.second_factor_at = None  # the session never proved a second factor
    await session.commit()
    org_id = await _customer(session, ops_settings, "Locked Co")

    for path in (f"orgs/{org_id}/inspect", f"orgs/{org_id}/messages"):
        r = await ops.get(f"/api/v1/ops/monitoring/{path}", headers=auth_headers(token))
        assert r.status_code == 403, f"{path} opened without step-up: {r.status_code}"
        assert "step_up" in r.text, r.text


async def test_inspection_is_read_only_and_changes_nothing(ops, session, ops_settings):
    """It looks. It does not score, restrict, or recommend - looking at an account must never be
    what makes something happen to it."""
    from app.models import MonitorSignal

    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Untouched Co")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))

    for path in (f"orgs/{org_id}/inspect", f"orgs/{org_id}/messages"):
        assert (
            await ops.get(f"/api/v1/ops/monitoring/{path}", headers=auth_headers(token))
        ).status_code == 200

    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=False)
    assert state is None or (state.level == "normal" and state.score == 0)
    signals = (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(MonitorSignal.org_id == org_id)
        )
    ).scalar_one()
    assert signals == 0


async def test_inspection_requires_an_operator(ops, session, ops_settings):
    org_id = await _customer(session, ops_settings, "Private Co")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))
    token = await register_and_login(ops, "outsider@example.com")
    for path in (f"orgs/{org_id}/inspect", f"orgs/{org_id}/messages"):
        r = await ops.get(f"/api/v1/ops/monitoring/{path}", headers=auth_headers(token))
        assert r.status_code == 403, f"{path} allowed a non-operator: {r.status_code}"


async def test_the_audit_sample_endpoint_is_admin_only_and_records_nothing_per_customer(
    ops, session, ops_settings
):
    """The miss-rate audit over HTTP. Admin, because it spends money rather than because it is
    dangerous: each sampled account is a full thorough review with real AI calls."""
    from app.models import MonitorHealth

    token = await _operator(ops, session)
    org_id = await _customer(session, ops_settings, "Quiet Book Co")
    await _traffic(session, org_id, at=datetime.now(timezone.utc))

    r = await ops.post(
        "/api/v1/ops/monitoring/audit-sample",
        json={"sample_size": 3, "days": 7, "seed": 11},
        headers=auth_headers(token),
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["sampled"] >= 1
    # No AI configured in this fixture, so nothing could be judged - and that must read as
    # "unverified", never as a clean platform.
    assert report["reviewed"] == 0
    assert report["miss_rate"] is None
    assert report["passed"] is False

    row = (
        await session.execute(
            sa.select(MonitorHealth).where(MonitorHealth.kind == "audit_sample")
        )
    ).scalar_one()
    assert row.passed is False

    # A reviewer may look at one account on request but may not commission a platform-wide sweep.
    reviewer = await _operator(ops, session, email="reviewer@example.com", role="reviewer")
    r = await ops.post(
        "/api/v1/ops/monitoring/audit-sample",
        json={"sample_size": 3},
        headers=auth_headers(reviewer),
    )
    assert r.status_code == 403, r.text
