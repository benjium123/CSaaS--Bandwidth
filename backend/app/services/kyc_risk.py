"""P41 risk tier for a business application.

Plain rules, each producing a sentence the reviewer (and, later, an auditor) can read. Any
single reason makes the application ``high`` risk. P43: a high-risk application needs every
document to fully match and gets a stricter AI review instead of a video call (operator
decision 2026-09-17); a human still approves it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.config import Settings
from app.models import KycCheck, KycPerson, KycProfile
from app.services import ban_list

#: Declared verticals that attract robocall and scam traffic. Not banned - reviewed harder.
HIGH_RISK_VERTICALS: frozenset[str] = frozenset(
    {
        "lead_generation",
        "debt_relief",
        "debt_collection",
        "auto_warranty",
        "crypto",
        "investment",
        "loans",
        "credit_repair",
        "insurance_leads",
        "medicare",
        "solar",
        "timeshare",
        "sweepstakes",
        "tech_support",
    }
)

HIGH_MONTHLY_CALLS = 50_000
HIGH_MONTHLY_TEXTS = 100_000
YOUNG_COMPANY_DAYS = 180


def evaluate(
    settings: Settings,
    profile: KycProfile,
    persons: list[KycPerson],
    checks: dict[str, KycCheck],
    *,
    today: date | None = None,
) -> tuple[str, list[str]]:
    today = today or datetime.now(timezone.utc).date()
    reasons: list[str] = []
    use_case = profile.use_case or {}

    if profile.incorporation_date is not None:
        age = (today - profile.incorporation_date).days
        if age < YOUNG_COMPANY_DAYS:
            reasons.append(f"Company was formed {age} days ago")

    website = checks.get("website")
    if website is not None and website.detail:
        domain_age = website.detail.get("domain_age_days")
        if isinstance(domain_age, int) and domain_age < 90:
            reasons.append(f"Website domain is {domain_age} days old")
    if website is None or website.result in ("warn", "fail"):
        reasons.append("Website check did not pass")

    email_domain = ban_list.domain_of(profile.business_email)
    if email_domain and email_domain in ban_list.FREE_EMAIL_DOMAINS:
        reasons.append("Business email is a free mailbox")

    for kind, label in (
        ("ban_list", "Matches the ban list"),
        ("sanctions", "Possible sanctions-list match"),
        ("registry", "Registry check did not pass"),
        ("name_match", "Name on an ID does not match"),
    ):
        check = checks.get(kind)
        if check is None or check.result not in ("warn", "fail"):
            continue
        # P43: most US states have no free registry feed; a company confirmed from its
        # registration document is normal, not a risk signal. A registry FAIL still is.
        if kind == "registry" and check.result == "warn" and (check.detail or {}).get(
            "from_documents"
        ):
            continue
        reasons.append(label)

    vertical = str(use_case.get("vertical") or "").strip().lower()
    if vertical in HIGH_RISK_VERTICALS:
        reasons.append(f"High-risk line of business: {vertical.replace('_', ' ')}")

    allowed = set(settings.kyc_country_list)
    destinations = {str(c).upper() for c in (use_case.get("destination_countries") or [])}
    outside = sorted(destinations - allowed)
    if outside:
        reasons.append("Plans to call or text outside US/CA/UK: " + ", ".join(outside))

    try:
        if int(use_case.get("monthly_calls") or 0) >= HIGH_MONTHLY_CALLS:
            reasons.append("Very high declared call volume")
        if int(use_case.get("monthly_texts") or 0) >= HIGH_MONTHLY_TEXTS:
            reasons.append("Very high declared text volume")
    except (TypeError, ValueError):
        reasons.append("Declared volume is not a number")

    if profile.submitted_from_flagged_login:
        reasons.append("Submitted from a flagged sign-in (VPN, new country or new device)")

    foreign_ids = [
        p.full_name
        for p in persons
        if p.document_country and p.document_country.upper() not in allowed
    ]
    if foreign_ids:
        reasons.append("ID issued outside US/CA/UK for: " + ", ".join(foreign_ids))

    ai = checks.get("ai_decision") or checks.get("ai_summary")
    if ai is not None and ai.detail and ai.detail.get("suggested_risk") == "high":
        reasons.append("AI review suggested high risk")

    documents = checks.get("documents")
    if documents is not None and documents.result in ("warn", "fail"):
        reasons.append("Documents did not fully match the application")

    # De-duplicate while keeping order.
    seen: set[str] = set()
    unique = [r for r in reasons if not (r in seen or seen.add(r))]
    return ("high" if unique else "standard"), unique
