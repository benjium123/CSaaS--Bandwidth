"""10DLC phase 5: the advisory "Check my campaign".

Three layers, none of which ever blocks a filing:

* ``rule_findings`` - pure code. Always included, so a prompt-injected sample can never
  talk the check out of a finding.
* ``url_findings`` - can the carrier's vetter open the links? SSRF-safe: http/https only,
  every hop's host must be public, redirects followed by hand (3 max).
* ``ai_findings`` - one ``ai_guard.judge`` pass. Unavailable -> ``None``, the caller reports
  ``ai: false`` and still returns the rule results.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx
import sqlalchemy as sa

from app.errors import ConflictError
from app.models import AuditLogEntry
from app.services import ai_guard
from app.services import audit as audit_svc
from app.services.ban_list import FREE_EMAIL_DOMAINS

CHECK_ACTION = "tendlc.campaign_check"
DAILY_LIMIT = 10
SEVERITIES = ("likely-rejection", "improve")
SHORTENERS = (
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly",
    "cutt.ly",
)
CONSENT_WORDS = (
    "website", "form", "checkbox", "text", "keyword", "sign", "verbal", "paper", "app", "opt",
)
MAX_REDIRECTS = 3
MAX_READ_BYTES = 64 * 1024

AI_SYSTEM = (
    "You review a US business texting (10DLC) campaign application the way a carrier vetter "
    "at TCR/CTIA would. Check: the brand is named in the sample messages; opt-out language "
    "(STOP) is present; the consent (opt-in) process is described concretely; no shortened "
    "links; the samples match the stated use case; no SHAFT content (sex, hate, alcohol, "
    "firearms, tobacco, cannabis) unless age-gated; the privacy policy states that mobile "
    "data is not shared with third parties. Answer with one JSON object: "
    '{"suggestions": [{"severity": "likely-rejection" or "improve", "field": "<campaign '
    'field name>", "why": "<under 300 characters>", "suggested_text": "<replacement text, '
    'under 500 characters, or empty>"}]}. At most 10 suggestions. Return an empty list when '
    "nothing needs fixing."
)


def _finding(severity: str, field: str, why: str, suggested: str = "") -> dict:
    return {"severity": severity, "field": field, "why": why, "suggested_text": suggested}


def rule_findings(brand, campaign) -> list[dict]:
    out: list[dict] = []
    name = (brand.name or "").strip()
    samples = [str(s) for s in (campaign.sample_messages or [])]
    for i, text in enumerate(samples, 1):
        if name and name.lower() not in text.lower():
            out.append(
                _finding(
                    "improve",
                    "sample_messages",
                    f"Sample message {i} does not name your business ({name}). Carriers expect "
                    "the sender to be identifiable.",
                    f"{name}: {text}"[:500],
                )
            )
    if not any("stop" in s.lower() for s in samples):
        out.append(
            _finding(
                "likely-rejection",
                "sample_messages",
                'No sample message tells people how to opt out. Add "Reply STOP to opt out."',
                "Reply STOP to opt out.",
            )
        )
    lowered = [s.lower() for s in samples]
    if any(
        re.search(rf"(?<![a-z0-9]){re.escape(d)}(?![a-z0-9])", s)
        for s in lowered
        for d in SHORTENERS
    ):
        out.append(
            _finding(
                "likely-rejection",
                "sample_messages",
                "A sample message uses a shortened link (bit.ly, tinyurl and similar). Carriers "
                "reject shorteners; use your own domain.",
            )
        )
    opt_in = (campaign.opt_in_process or "").strip()
    if not any(w in opt_in.lower() for w in CONSENT_WORDS):
        out.append(
            _finding(
                "likely-rejection",
                "opt_in_process",
                "The opt-in process never says how people give consent (a web form, a "
                "checkbox, a keyword text, a paper sign-up...).",
            )
        )
    if len(opt_in) < 40:
        out.append(
            _finding(
                "likely-rejection",
                "opt_in_process",
                "The opt-in process is very short. Describe where and how people agree to "
                "receive texts, step by step.",
            )
        )
    if not (campaign.privacy_policy_url or "").strip():
        out.append(
            _finding(
                "likely-rejection",
                "privacy_policy_url",
                "No privacy policy link. Carriers want one that says mobile data is not shared.",
            )
        )
    if "stop" not in (campaign.opt_out_message or "").lower():
        out.append(
            _finding(
                "improve",
                "opt_out_message",
                "The opt-out message does not mention STOP.",
                f"You are unsubscribed from {name or 'our'} texts. No more messages will be "
                "sent. Reply START to re-subscribe.",
            )
        )
    help_message = campaign.help_message or ""
    has_phone = len(re.sub(r"\D", "", help_message)) >= 7
    if not (has_phone or "@" in help_message or "http" in help_message.lower()):
        out.append(
            _finding(
                "improve",
                "help_message",
                "The help message gives no way to reach you (a phone number, email or link).",
            )
        )
    domain = (brand.email or "").rpartition("@")[2].strip().lower()
    if domain in FREE_EMAIL_DOMAINS:
        out.append(
            _finding(
                "improve",
                "brand.email",
                f"The business email is on a free domain ({domain}). A company address looks "
                "more legitimate to a vetter.",
            )
        )
    return out


async def _reachable(client: httpx.AsyncClient, url: str) -> bool:
    from app.services import kyc_checks

    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            return False
        if not await kyc_checks._public_host(host):
            return False
        try:
            async with client.stream(
                "GET", current, timeout=5.0, follow_redirects=False
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return False
                    current = urljoin(current, location)
                    continue
                read = 0
                async for chunk in resp.aiter_bytes():
                    read += len(chunk)
                    if read >= MAX_READ_BYTES:
                        break
                return resp.status_code < 400
        except httpx.HTTPError:
            return False
    return False


async def url_findings(urls: dict[str, str], *, client: httpx.AsyncClient) -> list[dict]:
    """``urls`` maps a field name (``brand.website``, ``privacy_policy_url``, ...) to a URL."""
    out: list[dict] = []
    for field, url in urls.items():
        url = (url or "").strip()
        if not url:
            continue
        if not await _reachable(client, url):
            severity = (
                "likely-rejection"
                if field in ("brand.website", "privacy_policy_url")
                else "improve"
            )
            out.append(_finding(severity, field, f"We could not open {url}"[:300]))
    return out


def _validate_ai(raw) -> list[dict]:
    items = raw.get("suggestions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity, field = item.get("severity"), item.get("field")
        why, text = item.get("why"), item.get("suggested_text", "")
        if severity not in SEVERITIES or not isinstance(field, str) or not field.strip():
            continue
        if not isinstance(why, str) or not why.strip() or len(why) > 300:
            continue
        if text is None:
            text = ""
        if not isinstance(text, str) or len(text) > 500:
            continue
        out.append(_finding(severity, field.strip()[:64], why.strip(), text))
        if len(out) == 10:
            break
    return out


async def ai_findings(settings, brand, campaign) -> list[dict] | None:
    fields = {
        "brand_name": brand.name,
        "brand_website": brand.website,
        "use_case": campaign.use_case,
        "description": campaign.description,
        "opt_in_process": campaign.opt_in_process,
        "sample_messages": list(campaign.sample_messages or []),
        "help_message": campaign.help_message,
        "opt_out_message": campaign.opt_out_message,
        "opt_in_message": campaign.opt_in_message,
        "privacy_policy_url": campaign.privacy_policy_url,
    }
    user = "Review this campaign application.\n" + ai_guard.data_block("campaign", fields)
    try:
        verdict = await ai_guard.judge(
            settings, task="tendlc_campaign_review", system=AI_SYSTEM, user=user, max_tokens=1200
        )
    except ai_guard.AIUnavailable:
        return None
    return _validate_ai(verdict.data)


async def check(
    session,
    settings,
    brand,
    campaign,
    *,
    actor_user_id: uuid.UUID | None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    used = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(AuditLogEntry)
            .where(
                AuditLogEntry.org_id == campaign.org_id,
                AuditLogEntry.action == CHECK_ACTION,
                AuditLogEntry.target_id == str(campaign.id),
                AuditLogEntry.created_at >= since,
            )
        )
    ).scalar_one()
    if used >= DAILY_LIMIT:
        raise ConflictError(
            "You can check this campaign 10 times a day. Try again tomorrow.",
            code="check_limit",
        )

    findings = rule_findings(brand, campaign)
    urls = {
        "brand.website": brand.website or "",
        "privacy_policy_url": campaign.privacy_policy_url or "",
        "terms_url": campaign.terms_url or "",
        "opt_in_proof_url": campaign.opt_in_proof_url or "",
    }
    if client is not None:
        findings += await url_findings(urls, client=client)
    else:
        async with httpx.AsyncClient() as own:
            findings += await url_findings(urls, client=own)
    ai = await ai_findings(settings, brand, campaign)
    findings += ai or []

    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for f in findings:
        key = (f["field"], f["why"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    unique.sort(key=lambda f: 0 if f["severity"] == "likely-rejection" else 1)

    audit_svc.record(
        session,
        campaign.org_id,
        action=CHECK_ACTION,
        target_type="campaign",
        target_id=str(campaign.id),
        actor_user_id=actor_user_id,
        detail={"suggestions": unique, "ai": ai is not None},
    )
    await session.commit()
    return {
        "suggestions": unique,
        "ai": ai is not None,
        "checks_left_today": DAILY_LIMIT - used - 1,
    }
