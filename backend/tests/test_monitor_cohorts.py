"""Template cohorts: does grouping actually make a blended scammer visible?

The thesis under test is narrow and falsifiable: a workspace sending hundreds of genuine
messages and four scams should produce a SMALL NUMBER of cohorts, of which the scam is one,
separable by cheap behavioural facts rather than by reading the words. If that holds, the AI
is asked ~5 questions a day instead of ~400 and each one carries enough context to answer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
