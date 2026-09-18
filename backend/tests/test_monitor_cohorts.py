"""Template cohorts: does grouping actually make a blended scammer visible?

The thesis under test is narrow and falsifiable: a workspace sending hundreds of genuine
messages and four scams should produce a SMALL NUMBER of cohorts, of which the scam is one,
separable by cheap behavioural facts rather than by reading the words. If that holds, the AI
is asked ~5 questions a day instead of ~400 and each one carries enough context to answer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Message, MessageThread, Org
from app.services import monitor_cohorts as mc

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# ======================================================================================
# The clustering primitive. These are pure and are the foundation everything else rests on.
# ======================================================================================
def test_personalised_variants_of_one_template_form_one_cohort():
    """The case exact hashing cannot do, and the reason this module exists. `body_hash` in
    monitor_text gives four different digests here; a template must give one cohort."""
    rows = [
        (uuid.uuid4(), f"Hi {name}, your parcel DHL-{n} is held. Pay £2.99: http://d-{n}.top/x",
         f"+1214555{1000+n}", NOW)
        for n, name in enumerate(["Dave", "Sara", "Mo", "Ruth"])
    ]
    cohorts = mc.cluster(rows)
    assert len(cohorts) == 1, [c.sample_body for c in cohorts]
    assert cohorts[0].size == 4
    assert cohorts[0].recipient_count == 4


def test_genuinely_different_templates_stay_apart():
    rows = [
        (uuid.uuid4(), "Your appointment with Dan is confirmed for Tuesday at 9am.",
         "+12145551001", NOW),
        (uuid.uuid4(), "Your appointment with Dan is confirmed for Friday at 2pm.",
         "+12145551002", NOW),
        (uuid.uuid4(), "URGENT: your account is suspended. Verify at http://bank-verify.top",
         "+12145551003", NOW),
        (uuid.uuid4(), "URGENT: your account is suspended. Verify at http://bank-secure.xyz",
         "+12145551004", NOW),
    ]
    cohorts = mc.cluster(rows)
    # What matters is that the scam template never merges with the appointment template.
    # (The two appointment variants DO merge, because <when> normalisation collapses the
    # weekday and the meridiem - see _WHEN.)
    scam = [c for c in cohorts if "urgent" in c.sample_body.lower()]
    appt = [c for c in cohorts if "appointment" in c.sample_body.lower()]
    assert len(scam) == 1 and scam[0].size == 2
    assert len(appt) == 1 and appt[0].size == 2
    assert scam[0].fingerprint != appt[0].fingerprint


def test_normalisation_collapses_links_numbers_and_money_but_keeps_structure():
    a = mc.normalise("Pay £2.99 now: http://a.top/x1 ref 99231")
    b = mc.normalise("Pay £7.50 now: http://b.xyz/z9 ref 44182")
    assert a == b, (a, b)
    # Structure still matters - same words, different order is a different template.
    assert mc.normalise("please confirm the invoice") != mc.normalise("invoice the confirm please")


def test_singletons_are_not_campaigns():
    """One-off messages to one person each are conversations, not campaigns. Bodies must be
    genuinely different - "unique message 1..5" all normalise to "unique message <num>",
    which is the same template and correctly forms ONE cohort."""
    bodies = [
        "can you send the invoice over when you get a chance",
        "running late sorry be there shortly",
        "the boiler part arrived we can fit it this week",
        "thanks for the review much appreciated",
        "no problem at all speak soon",
    ]
    rows = [(uuid.uuid4(), b, f"+121455510{n:02d}", NOW) for n, b in enumerate(bodies)]
    cohorts = [c for c in mc.cluster(rows) if c.size >= mc.MIN_COHORT]
    assert cohorts == []


def test_cohorts_come_back_largest_first():
    rows = [(uuid.uuid4(), "small template here", f"+1214555200{n}", NOW) for n in range(2)]
    rows += [
        (uuid.uuid4(), "the big campaign body goes here", f"+1214555300{n}", NOW)
        for n in range(6)
    ]
    cohorts = mc.cluster(rows)
    assert [c.size for c in cohorts] == [6, 2]


# ======================================================================================
# The whole thesis, against real rows: 300 genuine messages to known customers, 4 scams to
# strangers. The scam must separate WITHOUT anyone reading the text.
# ======================================================================================
async def _org(session) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Dan's Plumbing", slug=f"dans-{org_id.hex[:8]}"))
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def _send(session, org_id, *, body, to, at, status="delivered", inbound=False):
    thread = (
        await session.execute(
            MessageThread.__table__.select().where(
                MessageThread.org_id == org_id, MessageThread.contact_e164 == to
            )
        )
    ).first()
    if thread is None:
        t = MessageThread(
            id=uuid.uuid4(), org_id=org_id, our_e164="+12145550100", contact_e164=to,
            last_message_at=at,
        )
        session.add(t)
        await session.flush()
        thread_id = t.id
    else:
        thread_id = thread.id
    m = Message(
        id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="outbound",
        status=status, from_e164="+12145550100", to_e164=to, body=body, media=[],
        created_at=at,
    )
    session.add(m)
    if inbound:
        session.add(
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread_id, direction="inbound",
                status="received", from_e164=to, to_e164="+12145550100", body="thanks",
                media=[], created_at=at + timedelta(minutes=5),
            )
        )
    return m


async def test_four_scams_among_three_hundred_texts_separate_into_their_own_cohort(session):
    org_id = await _org(session)
    old = NOW - timedelta(days=30)

    # A real customer base: 60 people this workspace has messaged before and who reply.
    customers = [f"+1214555{2000+n}" for n in range(60)]
    for to in customers:
        await _send(session, org_id, body="Hi, Dan here. Booking confirmed.", to=to, at=old)
    await session.commit()

    # Today: 300 genuine appointment messages to those same customers, half of whom reply.
    for i, to in enumerate(customers * 5):
        await _send(
            session, org_id,
            body=f"Hi, your plumbing appointment is confirmed for {i % 7 + 1}pm today. Dan.",
            to=to, at=NOW, inbound=(i % 2 == 0),
        )
    # And four scams, to strangers, one undelivered because the list is stale.
    strangers = ["+19725558001", "+13055558002", "+16175558003", "+14155558004"]
    for n, to in enumerate(strangers):
        await _send(
            session, org_id,
            body=f"DHL: parcel {90000+n} held pending £2.99 fee. Pay: http://dhl-{n}.top/p",
            to=to, at=NOW, status="failed" if n == 3 else "delivered",
        )
    await session.commit()

    cohorts = await mc.build(session, org_id, since=NOW - timedelta(hours=1))

    # A day of traffic became a handful of questions, not 304.
    assert len(cohorts) <= 5, [(c.size, c.sample_body[:40]) for c, _ in cohorts]

    scam = [(c, m) for c, m in cohorts if "dhl" in c.sample_body.lower()]
    legit = [(c, m) for c, m in cohorts if "plumbing" in c.sample_body.lower()]
    assert len(scam) == 1, "the four scams did not form a single cohort"
    assert len(legit) == 1, "the genuine traffic did not form a single cohort"

    scam_c, scam_m = scam[0]
    legit_c, legit_m = legit[0]
    assert scam_c.size == 4
    assert legit_c.size == 300

    # THE POINT: the separation is behavioural, not textual.
    assert scam_m.first_contact_ratio == 1.0, "every scam recipient should be a stranger"
    assert legit_m.first_contact_ratio == 0.0, "every genuine recipient was known"
    assert scam_m.reply_rate == 0.0
    assert legit_m.reply_rate >= 0.4, legit_m
    assert legit_m.reply_rate > scam_m.reply_rate
    assert scam_m.undelivered_rate > 0.0
    assert scam_m.spread >= 4, "a purchased list spans area codes; a local trade does not"

    # And the cheap pre-filter picks the scam without an AI call, while leaving the
    # 300-message genuine campaign alone.
    assert mc.looks_like_a_campaign(scam_c, scam_m) is True
    assert mc.looks_like_a_campaign(legit_c, legit_m) is False


async def test_a_legitimate_blast_to_known_customers_is_not_flagged(session):
    """The false-positive case that matters: a real marketing send to a real list. Large,
    templated, low reply rate - and it must NOT trip the filter, because every recipient is
    someone this workspace has messaged before."""
    org_id = await _org(session)
    old = NOW - timedelta(days=30)
    customers = [f"+1214555{4000+n}" for n in range(40)]
    for to in customers:
        await _send(session, org_id, body="Welcome to Dan's Plumbing.", to=to, at=old)
    await session.commit()
    for to in customers:
        await _send(
            session, org_id,
            body="Spring boiler service, 10% off this month. Dan.", to=to, at=NOW,
        )
    await session.commit()

    cohorts = await mc.build(session, org_id, since=NOW - timedelta(hours=1))
    blast = [(c, m) for c, m in cohorts if "boiler" in c.sample_body.lower()]
    assert len(blast) == 1
    cohort, m = blast[0]
    assert cohort.size == 40
    assert m.first_contact_ratio == 0.0
    assert mc.looks_like_a_campaign(cohort, m) is False, (
        "a templated blast to an existing customer list must not be flagged as a campaign "
        f"worth an AI call: first_contact={m.first_contact_ratio} reply={m.reply_rate} "
        f"undelivered={m.undelivered_rate} spread={m.spread}"
    )


# ======================================================================================
# The AI half. What is under test is the LOAD property as much as the verdict: 304 messages
# must cost ONE AI call, not 304, and the call must carry enough context to answer.
# ======================================================================================
async def _plumber_with_a_side_scam(session, settings):
    """300 genuine appointment texts to known customers + 4 parcel scams to strangers."""
    from app.models import KycProfile

    org_id = await _org(session)
    session.add(
        KycProfile(
            id=uuid.uuid4(), org_id=org_id, status="approved", legal_name="Dan's Plumbing Ltd",
            website="dansplumbing.example",
            use_case={
                "vertical": "home_services",
                "description": "Booking confirmations and reminders for plumbing jobs",
                "who_you_contact": "Customers who booked a job",
            },
        )
    )
    old = NOW - timedelta(days=30)
    customers = [f"+1214555{2000 + n}" for n in range(60)]
    for to in customers:
        await _send(session, org_id, body="Hi, Dan here. Booking confirmed.", to=to, at=old)
    await session.commit()
    for i, to in enumerate(customers * 5):
        await _send(
            session, org_id,
            body=f"Hi, your plumbing appointment is confirmed for {i % 7 + 1}pm today. Dan.",
            to=to, at=NOW, inbound=(i % 2 == 0),
        )
    for n, to in enumerate(["+19725558001", "+13055558002", "+16175558003", "+14155558004"]):
        await _send(
            session, org_id,
            body=f"DHL: parcel {90000 + n} held pending £2.99 fee. Pay: http://dhl-{n}.top/p",
            to=to, at=NOW, status="failed" if n == 3 else "delivered",
        )
    await session.commit()
    return org_id


async def test_one_ai_call_for_the_campaign_not_one_per_message(session, monkeypatch):
    """The load claim, measured. 304 messages, and the AI is asked exactly once."""
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key")
    await _plumber_with_a_side_scam(session, settings)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL",
        "reason": "A plumbing business is sending courier parcel-fee notices to strangers.",
    }
    with ai.installed():
        result = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))

    cohort_calls = ai.tasks("reviewing a CAMPAIGN")
    assert len(cohort_calls) == 1, f"expected ONE cohort call, got {len(cohort_calls)}"
    assert result["reviewed"] == 1
    assert result["signals"] == 1

    # The one call it made was about the scam, not the 300 genuine messages.
    sent = cohort_calls[0]["messages"][1]["content"]
    assert "dhl" in sent.lower()
    assert "plumbing appointment is confirmed" not in sent


async def test_the_signal_is_a_decision_pack_not_just_a_score(session):
    """An operator must be able to decide without opening the message log - that is where the
    review-time half of the load saving comes from."""
    from app.models import MonitorSignal
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key")
    org_id = await _plumber_with_a_side_scam(session, settings)
    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from a plumber.",
    }
    with ai.installed():
        await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))

    set_org_context(session, org_id)
    signal = (
        await session.execute(
            MonitorSignal.__table__.select().where(MonitorSignal.kind == "cohort_inconsistent")
        )
    ).first()
    assert signal is not None, "a confident inconsistent verdict must record a signal"
    detail = signal.detail
    for key in (
        "template", "messages", "recipients", "first_contact_ratio", "reply_rate",
        "undelivered_rate", "spread", "impersonates", "confidence", "reason",
    ):
        assert key in detail, f"decision pack is missing {key}"
    assert detail["messages"] == 4
    assert detail["first_contact_ratio"] == 1.0
    assert detail["impersonates"] == "DHL"
    # Weight, not just presence: one confident cohort must NOT be enough to pause an account.
    assert signal.weight == mc.WEIGHT_INCONSISTENT
    assert signal.weight < 100


async def test_a_consistent_verdict_records_nothing(session):
    """The false-positive guard. If the AI says the campaign fits the business, no signal -
    otherwise every legitimate marketing send becomes an operator task."""
    from app.models import MonitorSignal
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key")
    await _plumber_with_a_side_scam(session, settings)
    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "consistent", "confidence": 88, "category": "none",
        "impersonates": None, "reason": "Delivery notices are normal for this business.",
    }
    with ai.installed():
        result = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))
    assert result["reviewed"] == 1
    assert result["signals"] == 0
    rows = (await session.execute(MonitorSignal.__table__.select())).all()
    assert rows == []


async def test_an_ai_outage_is_not_an_enforcement_decision(session):
    """A cohort that cannot be reviewed is counted and left alone. Failing closed here would
    mean a DeepSeek outage silently throttles every customer."""
    from app.models import MonitorSignal
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key")
    await _plumber_with_a_side_scam(session, settings)
    ai = FakeSafetyAI()
    ai.fail = True
    with ai.installed():
        result = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))
    assert result["unavailable"] == 1
    assert result["signals"] == 0
    assert (await session.execute(MonitorSignal.__table__.select())).all() == []


async def test_low_confidence_gets_a_lighter_weight(session):
    from app.models import MonitorSignal
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key")
    org_id = await _plumber_with_a_side_scam(session, settings)
    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 40, "category": "scam",
        "impersonates": None, "reason": "Possibly unrelated to the declared business.",
    }
    with ai.installed():
        await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))
    set_org_context(session, org_id)
    signal = (
        await session.execute(
            MonitorSignal.__table__.select().where(MonitorSignal.kind == "cohort_inconsistent")
        )
    ).first()
    assert signal is not None
    assert signal.weight == mc.WEIGHT_INCONSISTENT_WEAK


# ======================================================================================
# Repetition. The sweep runs hourly over a 24-hour window, so it sees the same campaign
# roughly 24 times - and the score is a rolling sum with no decay.
# ======================================================================================
async def test_the_same_campaign_is_not_re_signalled_on_the_next_sweep(session, monkeypatch):
    """Without dedupe this is the worst bug in the design, and it is arithmetic, not judgement.

    One scam campaign, weight 35, re-signalled on every hourly pass across a 24-hour window:
    105 points by the third hour, past the pause threshold of 100, from ONE campaign nobody
    looked at twice. The operator then opens a queue showing twenty-four campaigns where there
    was one - which is precisely the false volume the cohort design exists to remove. It also
    pays for an AI call every single time, so the load claim collapses with it.
    """
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                            deepseek_api_key="test-key", monitor_auto_action=True)
    org_id = await _plumber_with_a_side_scam(session, settings)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from a plumber.",
    }
    with ai.installed():
        first = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1))
        # The next hourly pass, same 24-hour window, same messages still inside it.
        second = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=2))

    assert first["signals"] == 1
    assert second["signals"] == 0, "the same campaign was signalled twice"
    assert second["reviewed"] == 0, "and it cost a second AI call to do it"
    assert second["repeats"] == 1, "the repeat should be counted, not silently dropped"
    # One AI call across BOTH passes: the dedupe is checked before the money is spent.
    assert len(ai.tasks("reviewing a CAMPAIGN")) == 1

    set_org_context(session, org_id)
    import sqlalchemy as sa

    from app.models import MonitorSignal

    rows = (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(
                MonitorSignal.org_id == org_id, MonitorSignal.kind == mc.SIGNAL_KIND
            )
        )
    ).scalar_one()
    assert rows == 1, f"{rows} signals on disk for one campaign"


async def test_a_rejected_campaign_does_not_reappear_on_the_next_sweep(session):
    """The operator's decision must outlive the arithmetic.

    When an operator rejects a recommendation ("I checked the messages, this IS their real
    business"), `cleared_before` stops the old signals counting. If the dedupe were narrowed to
    that same cutoff, the very next sweep would re-signal the identical template, the score
    would climb back, and the recommendation would be on the operator's desk again within the
    hour. So `signalled_fingerprints` deliberately spans the whole signal window and ignores
    `cleared_before` - "I checked this" buys quiet for the window, not for one tick.
    """
    from app.services import monitor_score
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                            deepseek_api_key="test-key")
    org_id = await _plumber_with_a_side_scam(session, settings)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from a plumber.",
    }
    with ai.installed():
        assert (await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=1)))[
            "signals"
        ] == 1

        # The operator rejects it, exactly as the decision endpoint does.
        set_org_context(session, org_id)
        state = await monitor_score.get_state(session, org_id, create=True)
        monitor_score.clear_recommendation(state)
        state.cleared_before = NOW + timedelta(minutes=2)
        state.score = 0
        await session.commit()

        after = await mc.tick(session, settings, hours=1, now=NOW + timedelta(minutes=3))

    assert after["signals"] == 0, "a rejected campaign was re-raised by the next sweep"
    assert after["repeats"] == 1
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=True)
    assert monitor_score.recommended_level(state) is None
    assert state.score == 0


async def test_a_build_reports_what_it_could_not_see(session):
    """`truncated` and `singletons` exist so no caller can present a partial read as a full one.

    This is the same failure as the headline bug in monitor_review: a function that cannot say
    "I only read the most recent N messages" hands its caller no way to avoid claiming it read
    everything.
    """
    org_id = await _org(session)
    # Twelve genuinely unrelated one-offs. They have to be unrelated in WORDING, not just in
    # subject: the first draft of this test used "a one-off note about job <n>" twelve times,
    # which is one template with a merge field and correctly clustered into a single cohort.
    one_offs = [
        "Running about twenty minutes late, sorry.",
        "Can you leave the side gate unlocked please?",
        "The part came in, I can fit it Thursday.",
        "Invoice attached, no rush on payment.",
        "That leak was the washer, not the pipe.",
        "Do you want me to take the old boiler away?",
        "Parking was a nightmare, I ended up round the corner.",
        "Your tenant says the pressure has dropped again.",
        "I've left the manual on the kitchen worktop.",
        "Quote is a bit higher because of the scaffolding.",
        "Happy to come back and check it next week.",
        "Give me a ring when you're home and I'll swing by.",
    ]
    for n, body in enumerate(one_offs):
        await _send(session, org_id, body=body, to=f"+1214555{4000 + n}", at=NOW)
    for n in range(4):
        await _send(session, org_id, body=f"Hi, your parcel DHL-{n} is held. Pay the fee.",
                    to=f"+1214555{5000 + n}", at=NOW)
    await session.commit()

    full = await mc.build(session, org_id, since=NOW - timedelta(hours=1))
    assert full.scanned == 16
    assert full.truncated is False
    assert full.singletons == 12, "the one-offs are a known blind spot and must be reported"
    assert len(full) == 1

    capped = await mc.build(session, org_id, since=NOW - timedelta(hours=1), limit=8)
    assert capped.truncated is True, "hitting the row cap must be visible to the caller"
    assert capped.scanned == 8


# ======================================================================================
# The operator's review button, and the one thing it must never do: reassure.
# These live here rather than in test_monitor_operator_control.py because they need the
# cohort fixtures above - the property under test is that monitor_review reports the cohort
# layer's blind spots honestly rather than reporting silence as safety.
# ======================================================================================
def _pin_review_clock(monkeypatch):
    """`review_account` reads the wall clock, and these fixtures are pinned to NOW. Widening
    `days` instead would make the tests pass only until the fixture date drifts far enough into
    the past, which is a test that expires."""
    from app.services import monitor_review

    monkeypatch.setattr(monitor_review, "_now", lambda: NOW + timedelta(minutes=1))


async def test_a_review_with_no_ai_does_not_report_a_clean_bill_of_health(session, monkeypatch):
    """The bug the peer proved with a probe, pinned.

    Before this, `review_account` counted every campaign FOUND as one reviewed, so with no AI
    key the operator pressed "review" and got back:
        headline: "No campaign contradicts this business (2 reviewed)"
        ai: {"available": False, "calls": 0, "skipped": 2}
    Two campaigns the model never saw, reported as two campaigns cleared. An operator reading
    the headline - which is the entire point of a headline - would close the case.
    """
    from app.services import monitor_review
    from tests.conftest import make_settings

    # No provider key: ai_guard.is_available() is False, exactly as during an outage or a
    # misconfigured deploy.
    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    org_id = await _plumber_with_a_side_scam(session, settings)
    _pin_review_clock(monkeypatch)

    report = await monitor_review.review_account(session, settings, org_id, days=30)

    assert report["ai"]["available"] is False
    assert report["campaigns"], "the campaigns were still found and still listed"
    assert report["campaigns_reviewed"] == 0
    assert report["conclusive"] is False, "nothing was judged, so nothing is settled"
    headline = report["headline"]
    assert "NOT REVIEWED" in headline, headline
    assert "unavailable" in headline.lower(), headline
    # The specific words that must not appear, because they are what a reader acts on.
    assert "no campaign contradicts" not in headline.lower(), headline
    assert report["actions_taken"] == [], "and it still must not act"


async def test_a_review_that_did_run_says_how_much_it_judged(session, monkeypatch):
    from app.services import monitor_review
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key", monitor_auto_action=False)
    org_id = await _plumber_with_a_side_scam(session, settings)
    _pin_review_clock(monkeypatch)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from a plumber.",
    }
    with ai.installed():
        report = await monitor_review.review_account(session, settings, org_id, days=30)

    assert report["campaigns_reviewed"] == len(report["campaigns"])
    assert report["conclusive"] is True
    assert "reviewed campaigns do not match this business" in report["headline"]
    assert report["coverage"]["messages_scanned"] == 304
    assert report["coverage"]["truncated"] is False
    # It records what it found, and still takes no action.
    assert report["signals_added"] >= 1
    assert report["actions_taken"] == []
    assert report["monitor"]["level"] != "paused"


async def test_a_second_review_does_not_double_the_score(session, monkeypatch):
    """The operator's button is idempotent per template. Pressing "thorough review" twice on
    the same account must not score the same campaign twice - otherwise the act of looking
    carefully at an account is itself enough to get it paused."""
    from app.services import monitor_review
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key", monitor_auto_action=False)
    org_id = await _plumber_with_a_side_scam(session, settings)
    _pin_review_clock(monkeypatch)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 92, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from a plumber.",
    }
    with ai.installed():
        first = await monitor_review.review_account(session, settings, org_id, days=30)
        second = await monitor_review.review_account(session, settings, org_id, days=30)

    assert first["signals_added"] >= 1
    assert second["signals_added"] == 0, "the second review scored the same campaign again"
    assert second["monitor"]["score"] == first["monitor"]["score"]
    flagged = [c for c in second["campaigns"] if c.get("already_on_record")]
    assert flagged, "the report should say which campaigns were already on the record"


# ======================================================================================
# The wiring. A detector with no caller detects nothing.
# ======================================================================================
async def test_the_sweeper_actually_runs_the_cohort_sweep(session, monkeypatch):
    """`tick()` existed, was tested, and was called by NOTHING for its whole life.

    Every other test in this file drives `tick` directly, which is exactly why that could go
    unnoticed: a unit test proves the function works, never that anything invokes it. In
    production the hourly sweeper is the only thing that would, so this asserts the sweeper
    reaches it - and asserts it through `run_once`, the real entry point, rather than by reading
    the job list.
    """
    from types import SimpleNamespace

    from app.services import monitor_exam
    from app.services import sweeper as sweeper_svc
    from tests.conftest import make_settings

    # This test drives the WHOLE sweeper pass, which includes other AI-bound jobs that do not
    # install the fake - and `backend/.env` holds a live DeepSeek key. A bogus key gets a 401
    # rather than a bill, but it is still a real outbound request from a unit test, and the
    # failure mode if the key were ever valid is silent and costs money rather than turning
    # anything red. Point the client at a closed port so nothing can leave the machine.
    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key",
                             deepseek_base_url="http://127.0.0.1:1")
    org_id = await _plumber_with_a_side_scam(session, settings)

    calls: list[dict] = []

    async def spy(_session, _settings, **kwargs):
        calls.append(kwargs)
        return {"orgs": 1, "cohorts": 2, "reviewed": 1, "signals": 1, "unavailable": 0,
                "repeats": 0}

    monkeypatch.setattr(mc, "tick", spy)
    # The same hourly block holds the canary and the WEEKLY EXAM, and the exam deliberately
    # runs in a task beside the sweeper (`asyncio.create_task`) because it takes minutes. Left
    # due, it outlives this test and dies mid-rollback during teardown, which shows up as a
    # CancelledError in an unrelated fixture. Nothing to do with the wiring under test.
    async def _not_due(*_a, **_k):
        return False

    monkeypatch.setattr(monitor_exam, "due", _not_due)
    fake_app = SimpleNamespace(state=SimpleNamespace(settings=settings, media_store=None))

    results = await sweeper_svc.run_once(fake_app)

    assert calls, "the sweeper never called the cohort sweep"
    assert calls[0]["hours"] == 24
    # The counts reach the pass log flattened, not as a nested dict - a dict is always truthy
    # and `any(results.values())` would then announce every hourly pass as eventful. (Other
    # jobs in this pass do return structures, so this checks the cohort keys specifically.)
    assert results["monitor_campaign_signals"] == 1
    assert results["monitor_campaigns"] == 1
    assert not any(
        isinstance(v, dict) for k, v in results.items() if k.startswith("monitor_campaign")
    )

    # Hourly gate: an immediate second pass must not run it again.
    await sweeper_svc.run_once(fake_app)
    assert len(calls) == 1, "the cohort sweep ran twice inside one hour"
    assert org_id is not None


# ======================================================================================
# The scammer who buys three accounts. The one pattern a per-account lens cannot see.
# ======================================================================================
async def test_a_victim_list_split_across_workspaces_is_counted_not_named(session, monkeypatch):
    """Two workspaces, the same strangers, neither remarkable on its own.

    This is the gap the whole rest of the module has by construction: every other signal here
    is computed inside one org, and an operation that splits one list across three accounts
    gives each account a modest campaign to a modest number of strangers. The overlap is the
    only thing that betrays it.

    And it must stay a COUNT. The second workspace's identity, numbers and message bodies must
    never appear in the first one's review, or the safety feature becomes the tenancy leak.
    """
    from app.services import monitor_review
    from tests.conftest import make_settings

    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    victims = [f"+1972555{7000 + n}" for n in range(12)]

    first = await _org(session)
    for to in victims:
        await _send(session, first, body=f"DHL: parcel held, pay the fee. {to[-3:]}", to=to,
                    at=NOW)
    await session.commit()

    # A second workspace, working the same list with a different template.
    second = uuid.uuid4()
    session.add(Org(id=second, name="Other Ltd", slug=f"other-{second.hex[:8]}"))
    await session.commit()
    # The write guard refuses a row whose org_id is not the session's context, which is the
    # tenancy boundary doing its job - the fixture has to switch orgs explicitly.
    set_org_context(session, second)
    for to in victims[:9]:
        await _send(session, second, body=f"USPS: redelivery fee outstanding. {to[-3:]}", to=to,
                    at=NOW)
    await session.commit()

    _pin_review_clock(monkeypatch)
    report = await monitor_review.review_account(session, settings, first, days=30)

    campaign = report["campaigns"][0]
    overlap = campaign["shared_with_other_workspaces"]
    assert overlap["recipients_checked"] == 12
    assert overlap["also_contacted_elsewhere"] == 9, overlap
    assert overlap["other_workspaces"] == 1

    # Nothing identifying the other workspace crosses over. Checked against the whole report,
    # not just the overlap block, because a leak would not politely stay in its own field.
    blob = repr(report)
    assert str(second) not in blob
    assert "Other Ltd" not in blob
    assert "USPS" not in blob, "another workspace's message body reached this report"


async def test_overlap_is_evidence_and_never_a_score(session, monkeypatch):
    """A shared recipient list is not proof of anything - bought lead lists are ordinary, and a
    consumer legitimately hears from several businesses. Scoring it would score a customer for
    their supplier's behaviour, so it must not move the needle on its own."""
    from app.models import MonitorSignal
    from app.services import monitor_review
    from tests.conftest import make_settings

    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    shared = [f"+1305555{8000 + n}" for n in range(10)]

    a = await _org(session)
    for to in shared:
        await _send(session, a, body="Your MOT is due this month, book online.", to=to, at=NOW)
    await session.commit()
    b = uuid.uuid4()
    session.add(Org(id=b, name="Garage Two", slug=f"g2-{b.hex[:8]}"))
    await session.commit()
    set_org_context(session, b)
    for to in shared:
        await _send(session, b, body="Service reminder: your car is due a check.", to=to, at=NOW)
    await session.commit()

    _pin_review_clock(monkeypatch)
    report = await monitor_review.review_account(session, settings, a, days=30)

    assert report["campaigns"][0]["shared_with_other_workspaces"][
        "also_contacted_elsewhere"
    ] == 10
    assert report["signals_added"] == 0
    set_org_context(session, a)
    signals = (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(MonitorSignal.org_id == a)
        )
    ).scalar_one()
    assert signals == 0, "overlap must not write a signal"
    assert report["monitor"]["level"] == "normal"


# ======================================================================================
# Measuring the MISS rate. Every other number here measures what was caught.
# ======================================================================================
async def _quiet_account_with_a_campaign(session, settings, name, *, body, count, at):
    """An account with real outbound and no monitor findings at all - the kind the monitor is
    silent on, which is exactly the population a miss hides in."""
    org_id = uuid.uuid4()
    slug = f"{name.lower().replace(chr(32), chr(45))}-{org_id.hex[:6]}"
    session.add(Org(id=org_id, name=name, slug=slug))
    await session.commit()
    set_org_context(session, org_id)
    stem = str(int(org_id.int % 900 + 100))
    for n in range(count):
        await _send(
            session, org_id, body=f"{body} ref {90000 + n}",
            to=f"+1972{stem}{n:04d}", at=at,
        )
    await session.commit()
    return org_id


async def test_the_audit_looks_where_the_monitor_says_there_is_nothing(session, monkeypatch):
    """The only number in this system that can get worse while every other one looks healthy.

    Signals raised, campaigns flagged, exam cases stopped - none of them move when the monitor
    stops SEEING something. A template that drifts below the pre-filter, or an account nobody had
    a reason to open, produces exactly the reporting a clean platform does. "No findings" and
    "not looking" are indistinguishable from the inside, so this samples accounts the monitor
    believes are fine and reviews them properly.
    """
    from app.models import MonitorHealth
    from app.services import monitor_review
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key", monitor_auto_action=False)
    scammer = await _quiet_account_with_a_campaign(
        session, settings, "Quiet Scammer",
        body="DHL: your parcel is held pending a fee. Pay now.", count=6, at=NOW,
    )
    _pin_review_clock(monkeypatch)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 93, "category": "impersonation",
        "impersonates": "DHL", "reason": "Courier parcel-fee notices from an unrelated business.",
    }
    with ai.installed():
        report = await monitor_review.audit_sample(
            session, settings, sample_size=3, days=7, seed=1, now=NOW + timedelta(minutes=1)
        )

    assert report["sampled"] >= 1
    assert report["reviewed"] >= 1
    assert report["missed"] == 1, report
    assert report["miss_rate"] == round(1 / report["reviewed"], 3)
    assert report["passed"] is False
    found = report["findings"][0]
    assert found["org_id"] == str(scammer)
    assert found["campaigns"][0]["reason"]

    # The result is a tracked series, not a number that scrolls past in a log.
    rows = (
        (
            await session.execute(
                sa.select(MonitorHealth).where(MonitorHealth.kind == "audit_sample")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].passed is False
    assert rows[0].detail["missed"] == 1


async def test_the_audit_scores_nothing_and_tells_the_customer_nothing(session, monkeypatch):
    """Two properties that make this a measurement rather than a sweep.

    It must not score the accounts it samples - an audit that flagged what it sampled would change
    the thing it measures, and being sampled would become a reason to be flagged. And it must not
    write into a sampled customer's own audit log, because that log is visible to them: it would
    tell a customer they had been picked for a scam audit, which is both a tip-off and, for the
    innocent majority, simply untrue as a statement about them.
    """
    from app.models import MonitorSignal
    from app.models.platform import AuditLogEntry
    from app.services import monitor_review, monitor_score
    from tests.conftest import make_settings
    from tests.fake_ai import FakeSafetyAI

    settings = make_settings(monitor_enforced=True, ai_guard_enabled=True,
                             deepseek_api_key="test-key", monitor_auto_action=False)
    org_id = await _quiet_account_with_a_campaign(
        session, settings, "Sampled Co",
        body="URGENT: your bank account is suspended, verify now.", count=6, at=NOW,
    )
    _pin_review_clock(monkeypatch)

    ai = FakeSafetyAI()
    ai.generic = {
        "verdict": "inconsistent", "confidence": 95, "category": "phishing",
        "impersonates": "a bank", "reason": "Bank-impersonation phishing.",
    }
    with ai.installed():
        report = await monitor_review.audit_sample(
            session, settings, sample_size=3, days=7, seed=7, now=NOW + timedelta(minutes=1)
        )
    assert report["missed"] == 1, "the fixture must actually be caught, or this proves nothing"

    set_org_context(session, org_id)
    signals = (
        await session.execute(
            sa.select(sa.func.count(MonitorSignal.id)).where(MonitorSignal.org_id == org_id)
        )
    ).scalar_one()
    assert signals == 0, "the audit scored an account it merely sampled"
    state = await monitor_score.get_state(session, org_id, create=False)
    assert state is None or (state.level == "normal" and state.score == 0)

    rows = (
        (
            await session.execute(
                sa.select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [], "a sampled customer must not be able to see that they were sampled"


async def test_an_audit_that_could_review_nothing_does_not_pass(session, monkeypatch):
    """The failure this whole function exists to detect, which it must not commit itself.

    With no AI reachable, nothing can be judged - and a `missed == 0` computed over zero reviews
    is the same vacuous pass as a catch rate over zero cases. `passed` therefore requires that
    something was actually reviewed, and `miss_rate` stays None rather than becoming 0.0, because
    0.0 reads as "we checked and found nothing".
    """
    from app.models import MonitorHealth
    from app.services import monitor_review
    from tests.conftest import make_settings

    # No provider key: ai_guard.is_available() is False.
    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    await _quiet_account_with_a_campaign(
        session, settings, "Unreviewable Co",
        body="DHL: your parcel is held pending a fee.", count=6, at=NOW,
    )
    _pin_review_clock(monkeypatch)

    report = await monitor_review.audit_sample(
        session, settings, sample_size=3, days=7, seed=3, now=NOW + timedelta(minutes=1)
    )

    assert report["sampled"] >= 1
    assert report["reviewed"] == 0
    assert report["missed"] == 0
    assert report["miss_rate"] is None, "0.0 would read as 'checked, found nothing'"
    assert report["passed"] is False, "an audit that reviewed nothing has not passed"
    assert report["inconclusive"] >= 1
    row = (
        await session.execute(
            sa.select(MonitorHealth).where(MonitorHealth.kind == "audit_sample")
        )
    ).scalar_one()
    assert row.passed is False


async def test_the_audit_ignores_accounts_the_monitor_already_flagged(session, monkeypatch):
    """The miss rate is about accounts the monitor is SILENT on. An account already carrying a
    finding, or already above `normal`, is in front of a human by another route - counting it
    here would flatter the number with cases the system did not miss."""
    from app.services import monitor_review, monitor_score
    from tests.conftest import make_settings

    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    flagged = await _quiet_account_with_a_campaign(
        session, settings, "Already Known",
        body="DHL: your parcel is held pending a fee.", count=6, at=NOW,
    )
    await monitor_score.add_signal(
        session, settings, flagged, "text_blocked", "already caught", weight=40
    )
    await session.commit()
    _pin_review_clock(monkeypatch)

    report = await monitor_review.audit_sample(
        session, settings, sample_size=5, days=7, seed=5, now=NOW + timedelta(minutes=1)
    )
    assert report["eligible_accounts"] == 0, "an already-flagged account is not a candidate"
    assert report["sampled"] == 0
    assert report["miss_rate"] is None
    assert report["passed"] is False


async def test_the_audit_leaves_no_row_on_an_account_it_merely_sampled(session, monkeypatch):
    """The promise in the docstring was false, and the write was in the READ.

    `review_account` called `get_state(create=True)`, which INSERTs an empty `OrgMonitoring` row
    for any account that has never been monitored. So a sampled account acquired a row whose
    `created_at` records the moment it was audited - "was I sampled?" answerable from the
    database, from a function documented as writing nothing. `record_signals=False` never covered
    it, because a signal was not what got written.
    """
    from app.models import OrgMonitoring
    from app.services import monitor_review
    from tests.conftest import make_settings

    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    org_id = await _quiet_account_with_a_campaign(
        session, settings, "Untouched By Audit",
        body="DHL: your parcel is held pending a fee.", count=6, at=NOW,
    )
    _pin_review_clock(monkeypatch)

    before = (
        await session.execute(sa.select(sa.func.count(OrgMonitoring.id)))
    ).scalar_one()
    report = await monitor_review.audit_sample(
        session, settings, sample_size=3, days=7, seed=2, now=NOW + timedelta(minutes=1)
    )
    assert report["sampled"] >= 1, "the fixture must actually be sampled, or this proves nothing"
    after = (
        await session.execute(sa.select(sa.func.count(OrgMonitoring.id)))
    ).scalar_one()
    assert after == before, "the audit created a monitoring row for an account it only read"

    set_org_context(session, org_id)
    assert (
        await session.execute(
            sa.select(OrgMonitoring).where(OrgMonitoring.org_id == org_id)
        )
    ).scalar_one_or_none() is None

    # And an account with no row still reads as "the monitor has no opinion" rather than as
    # missing data - which is the literally true statement about it.
    one = await monitor_review.review_account(
        session, settings, org_id, days=7, record_signals=False, touch_state=False
    )
    assert one["monitor"] == {
        "level": "normal",
        "score": 0,
        "recommendation": None,
        "paused_reason": None,
    }


async def test_a_requested_sample_size_is_honoured_not_floored_by_the_bands(session, monkeypatch):
    """`sample_size // 3` gave one per band, so an operator who asked for five got three - with
    the report saying `sampled: 3`, so nothing lied and nothing explained it either."""
    from app.services import monitor_review
    from tests.conftest import make_settings

    settings = make_settings(monitor_enforced=True, monitor_auto_action=False)
    for n in range(9):
        await _quiet_account_with_a_campaign(
            session, settings, f"Book Co {n}",
            body=f"Reminder number {n} about your booking with us this week.", count=3, at=NOW,
        )
    _pin_review_clock(monkeypatch)

    for asked in (1, 2, 5, 8):
        report = await monitor_review.audit_sample(
            session, settings, sample_size=asked, days=7, seed=4,
            now=NOW + timedelta(minutes=1),
        )
        assert report["sampled"] == asked, f"asked for {asked}, sampled {report['sampled']}"


def test_the_miss_rate_carries_its_precision():
    """A rate without its sample size is a claim, not a measurement - and this one always errs
    towards "the platform looks clean". One-sided Clopper-Pearson, checked against the closed
    form for the zero-miss case: 1 - 0.05**(1/n)."""
    from app.services.monitor_review import miss_rate_upper_bound

    assert miss_rate_upper_bound(0, 9) == round(1 - 0.05 ** (1 / 9), 3) == 0.283
    assert miss_rate_upper_bound(0, 25) == round(1 - 0.05 ** (1 / 25), 3)
    # More reviews, tighter bound - the whole reason to report it.
    assert miss_rate_upper_bound(0, 50) < miss_rate_upper_bound(0, 9)
    # A miss found: the bound sits above the point estimate, never below it.
    assert miss_rate_upper_bound(1, 9) > 1 / 9
    # Degenerate cases say "we know nothing" rather than something reassuring.
    assert miss_rate_upper_bound(0, 0) == 1.0
    assert miss_rate_upper_bound(9, 9) == 1.0
