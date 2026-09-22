"""P43: the AI decision pack - everything the operator needs to decide in one click.

The safety AI reads the whole application (business, use case, people, every automatic
check, every document review, the website) and writes: a recommendation, how confident it
is, its review in plain language, its thinking per check, concerns with evidence, questions
to put to the applicant, and suggested starting limits.

It NEVER decides. Approval stays a human click behind ``kyc.approval_blockers``; nothing in
this module changes an application's status. (Operator decision 2026-09-17.)

The pack is regenerated whenever its inputs change (input fingerprint), so the operator never
reads a recommendation based on stale checks.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, countries_phrase
from app.models import KycCheck, KycDocument, KycProfile, Org
from app.services import ai_guard, kyc_checks

RECOMMENDATIONS = ("approve", "needs_info", "reject")
ERROR_BACKOFF = timedelta(minutes=15)

SYSTEM = """You are the senior compliance analyst for a telecom company that sells phone
numbers, calling and texting to businesses in {countries}. Scammers try to sign up
as legitimate-looking businesses, so you review applications carefully, but you also don't
want to turn away genuine small businesses over paperwork noise.

You prepare a decision for a human reviewer, who makes the final call. Weigh:
- identity: owners verified with ID + selfie, names matching, proof of address matching
- the company: registry result, registration documents, age, website, email domain
- the use case: does what they say they'll do fit the business and its website? volumes,
  who they contact and where their contact lists come from (bought lists = red flag)
- screening: sanctions, ban list, flagged sign-ins
- anything that looks inconsistent across the application

Recommend "approve" only when identity and the company are confirmed and the use case is
coherent. Recommend "needs_info" when something fixable is missing or unclear. Recommend
"reject" for sanctions/ban-list failures, forged or mismatched documents, or a use case
that looks like scam/robocall traffic.

Return JSON with exactly these keys:
{
  "recommendation": "approve" | "needs_info" | "reject",
  "confidence": integer 0-100,
  "summary": "3-6 plain-language sentences a busy reviewer can act on",
  "thoughts": [{"area": "identity"|"company"|"documents"|"use_case"|"screening"|"website",
                "assessment": "ok"|"concern"|"unclear", "thought": "one or two sentences"}],
  "concerns": [{"concern": "short", "evidence": "what in the application shows it"}],
  "questions_for_applicant": ["specific question to ask, if more info is needed"],
  "suggested_risk": "standard" | "high",
  "suggested_limits": {"daily_calls": integer, "daily_texts": integer, "max_numbers": integer},
  "note_for_decision": "one sentence to record with the approval or rejection"
}"""

INDIVIDUAL_SYSTEM = """You are the senior compliance analyst for a telecom company that
sells phone numbers and calling to individuals in {countries}. Scammers try to
sign up as legitimate-looking people, so you review applications carefully, but you also
don't want to turn away genuine people over paperwork noise.

You prepare a decision for a human admin, who makes the final call. Weigh:
- identity: the person verified with ID + selfie, the name on the ID matching the name
  entered, and any proof of address matching. Proof of address is optional: if one was
  supplied, check that it matches; if none was supplied, that is never a concern and must
  not be raised as one
- screening: sanctions, ban list, flagged sign-ins
- the declared use case: does what they say they'll do fit a single person? volumes, who
  they contact and where their contact lists come from (bought lists = red flag)
- anything that looks inconsistent across the application

There is no company, no registry and no registration documents to check - do not ask for
them and do not treat their absence as a concern. Identity approval enables calling.
Messaging additionally requires separate company/10DLC carrier registration. Suggest
texting limits based on risk, not the account type; campaign approval is enforced separately.

You only advise. Never change the application's status yourself - a human admin makes the
final decision.

Return JSON with exactly these keys:
{
  "recommendation": "approve" | "needs_info" | "reject",
  "confidence": integer 0-100,
  "summary": "3-6 plain-language sentences a busy reviewer can act on",
  "thoughts": [{"area": "identity"|"company"|"documents"|"use_case"|"screening"|"website",
                "assessment": "ok"|"concern"|"unclear", "thought": "one or two sentences"}],
  "concerns": [{"concern": "short", "evidence": "what in the application shows it"}],
  "questions_for_applicant": ["specific question to ask, if more info is needed"],
  "suggested_risk": "standard" | "high",
  "suggested_limits": {"daily_calls": integer, "daily_texts": integer, "max_numbers": integer},
  "note_for_decision": "one sentence to record with the approval or rejection"
}"""


def _clip(value: object, limit: int) -> object:
    if isinstance(value, str):
        return value[:limit]
    return value


async def build_application(session: AsyncSession, settings: Settings, profile: KycProfile) -> dict:
    from app.services import kyc as kyc_svc

    org = await session.get(Org, profile.org_id)
    account_type = org.account_type if org is not None else "business"
    persons = await kyc_checks.persons_for(session, profile.org_id)
    checks = await kyc_checks.latest_checks(session, profile.org_id)
    documents = (
        (
            await session.execute(
                sa.select(KycDocument)
                .where(KycDocument.org_id == profile.org_id)
                .order_by(KycDocument.created_at)
            )
        )
        .scalars()
        .all()
    )
    website = checks.get("website")
    return {
        "account_type": account_type,
        "business": {
            "country": profile.country,
            "legal_name": profile.legal_name,
            "dba_name": profile.dba_name,
            "entity_type": profile.entity_type,
            "registration_number": profile.registration_number,
            "incorporation_date": (
                profile.incorporation_date.isoformat() if profile.incorporation_date else None
            ),
            "registered_address": profile.registered_address,
            "website": profile.website,
            "business_email": profile.business_email,
            "business_phone": profile.business_phone,
        },
        "use_case": profile.use_case,
        "people": [
            {
                "role": p.role,
                "declared_name": p.full_name,
                "ownership_percent": p.ownership_percent,
                "id_status": p.status,
                "verified_name": p.verified_name,
                "id_document_country": p.document_country,
                "has_residential_address": bool(p.residential_address),
            }
            for p in persons
        ],
        "automatic_checks": {
            kind: {"result": c.result, "summary": c.summary}
            for kind, c in checks.items()
            if kind not in ("ai_summary", "ai_decision")
        },
        "documents": [
            {
                "kind": d.kind,
                "for_person": next((p.full_name for p in persons if p.id == d.person_id), None),
                "review_result": d.review_result,
                "reasons": (d.review or {}).get("reasons"),
                "read": {
                    k: v
                    for k, v in ((d.review or {}).get("read") or {}).items()
                    if k
                    in (
                        "document_type",
                        "issuer",
                        "document_date",
                        "company_name",
                        "company_status",
                        "looks_edited",
                        "notes",
                    )
                },
            }
            for d in documents
        ],
        # The AI's own earlier opinion is left out, or every pack would trigger the next one.
        "risk": {
            "reasons": [
                r for r in (profile.risk_reasons or []) if r != "AI review suggested high risk"
            ],
        },
        "submitted_from_flagged_login": profile.submitted_from_flagged_login,
        "approval_blockers": await kyc_svc.approval_blockers(session, profile),
        "website_excerpt": {
            "title": _clip((website.detail or {}).get("title"), 200) if website else None,
            "text": _clip((website.detail or {}).get("text_excerpt"), 3000) if website else None,
        },
    }


def fingerprint(application: dict) -> str:
    return hashlib.sha256(json.dumps(application, sort_keys=True, default=str).encode()).hexdigest()


def _normalize(data: dict) -> dict:
    recommendation = data.get("recommendation")
    if recommendation not in RECOMMENDATIONS:
        raise ai_guard.AIUnavailable("AI decision had no valid recommendation")
    try:
        confidence = max(0, min(100, int(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0
    limits = data.get("suggested_limits") if isinstance(data.get("suggested_limits"), dict) else {}

    def _int(key: str) -> int | None:
        try:
            value = int(limits.get(key))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def _list(key: str, limit: int) -> list:
        value = data.get(key)
        return value[:limit] if isinstance(value, list) else []

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "summary": str(data.get("summary") or "")[:2000],
        "thoughts": [
            {
                "area": str(t.get("area") or "")[:32],
                "assessment": str(t.get("assessment") or "")[:16],
                "thought": str(t.get("thought") or "")[:500],
            }
            for t in _list("thoughts", 10)
            if isinstance(t, dict)
        ],
        "concerns": [
            {
                "concern": str(c.get("concern") or "")[:300],
                "evidence": str(c.get("evidence") or "")[:500],
            }
            for c in _list("concerns", 10)
            if isinstance(c, dict)
        ],
        "questions_for_applicant": [str(q)[:300] for q in _list("questions_for_applicant", 8)],
        "suggested_risk": "high" if data.get("suggested_risk") == "high" else "standard",
        "suggested_limits": {
            "daily_calls": _int("daily_calls"),
            "daily_texts": _int("daily_texts"),
            "max_numbers": _int("max_numbers"),
        },
        "note_for_decision": str(data.get("note_for_decision") or "")[:500],
    }


async def latest_decision(session: AsyncSession, org_id: uuid.UUID) -> KycCheck | None:
    return (
        await session.execute(
            sa.select(KycCheck)
            .where(KycCheck.org_id == org_id, KycCheck.kind == "ai_decision")
            .order_by(KycCheck.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def generate_if_stale(
    session: AsyncSession, settings: Settings, profile: KycProfile
) -> KycCheck | None:
    """Write a fresh decision pack when the application changed since the last one."""
    application = await build_application(session, settings, profile)
    digest = fingerprint(application)
    previous = await latest_decision(session, profile.org_id)
    if previous is not None and (previous.detail or {}).get("input_hash") == digest:
        if previous.result != "error":
            return None
        # The AI failed on this exact application recently: back off instead of calling it
        # again every two minutes.
        created = previous.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is not None and datetime.now(timezone.utc) - created < ERROR_BACKOFF:
            return None
    system = INDIVIDUAL_SYSTEM if application.get("account_type") == "individual" else SYSTEM
    try:
        judgement = await ai_guard.judge(
            settings,
            task="kyc_decision",
            system=system.replace("{countries}", countries_phrase(settings.kyc_country_list)),
            user=ai_guard.data_block("application", application)
            + "\n\nPrepare the decision for the reviewer.",
            max_tokens=1500,
        )
        pack = _normalize(judgement.data)
    except ai_guard.AIUnavailable as exc:
        if previous is not None and previous.result == "error":
            previous.created_at = datetime.now(timezone.utc)  # restart the back-off
            return None  # don't pile up error rows every tick
        return kyc_checks._record(
            session,
            profile,
            "ai_decision",
            "error",
            "The AI decision pack could not be prepared - it will be retried",
            {"error": str(exc)[:200], "input_hash": digest},
        )
    result = {"approve": "pass", "needs_info": "warn", "reject": "fail"}[pack["recommendation"]]
    row = kyc_checks._record(
        session,
        profile,
        "ai_decision",
        result,
        pack["summary"][:500] or f"AI recommends: {pack['recommendation']}",
        {**pack, "input_hash": digest, "model": judgement.model},
    )
    row.tokens_in = judgement.tokens_in
    row.tokens_out = judgement.tokens_out
    return row
