"""P44a: where a workspace may call, text and fax.

The single biggest loss a telephony reseller can take is International Revenue Share Fraud:
someone gets into an account and pumps minutes to premium or satellite ranges whose carrier
kicks back a share. Nothing before this module looked at the destination - ``to_e164``
happily accepted +881, Cuba or a Jamaican lottery-scam number.

The policy, in order (first match wins):

1. Emergency short codes (911, 933, 999, 112) are ALWAYS allowed. Never block a 911 call.
2. HARD BLOCK, never overridable: satellite / international non-geographic codes
   (+870, +878, +881, +882, +883, +888, +979, +800, +808), the major IRSF country codes
   (Cuba, the Pacific islands, the usual African and South Atlantic ranges), the Caribbean
   NANP countries that Wangiri and callback scams live on, and every premium-rate,
   shared-cost, personal and pager number in ANY country (US 1-900, UK 09/087/084/070/076).
3. HOME REGION ONLY: a workspace reaches its own country and nothing else. International
   calling is disabled platform-wide (operator decision 2026-09-25) - there is deliberately
   no per-workspace override yet.
   - US workspace: the contiguous 48 states only. Alaska (907), Hawaii (808), Canada and the
     US territories (PR, VI, GU, MP, AS) are refused.
   - UK workspace: +44 numbers phonenumbers assigns to GB only. Crown-dependency ranges
     (Guernsey, Jersey, Isle of Man) share +44 but are a known IRSF target and are refused.

The home region is the workspace's verified country (``phone_region.for_org``), US until it
has one. Applies to calls, transfers, texts and faxes alike.

Refusal codes: ``destination_blocked`` (the number itself is never allowed) and
``destination_not_allowed`` (a real number outside the workspace's home region).
"""

from __future__ import annotations

import uuid

import phonenumbers
from phonenumbers import PhoneNumberType
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

BLOCKED = "destination_blocked"
NOT_ALLOWED = "destination_not_allowed"

EMERGENCY_NUMBERS = frozenset({"911", "933", "999", "112"})

#: International non-geographic ranges: satellite (Inmarsat, Iridium, Thuraya...),
#: International Networks, UIFN freephone/shared-cost, IPRS. Classic IRSF.
NON_GEOGRAPHIC_COUNTRY_CODES = frozenset({800, 808, 870, 878, 881, 882, 883, 888, 979})

#: Country codes that dominate IRSF / premium-termination fraud reports. Refused even if
#: international calling is ever turned on.
IRSF_COUNTRY_CODES = frozenset(
    {
        53,  # Cuba
        220, 222, 224, 231, 232, 235, 239, 245, 247, 252, 253, 269, 290, 291,
        # Gambia, Mauritania, Guinea, Liberia, Sierra Leone, Chad, Sao Tome, Guinea-Bissau,
        # Ascension, Somalia, Djibouti, Comoros, St Helena, Eritrea
        246,  # Diego Garcia
        500,  # Falkland Islands
        670, 672, 673, 674, 675, 676, 677, 678, 679, 680, 681, 682, 683, 685, 686, 687,
        688, 689, 690, 691, 692,
        # East Timor, Norfolk/Antarctica, Brunei, Nauru, PNG, Tonga, Solomon Is., Vanuatu,
        # Fiji, Palau, Wallis & Futuna, Cook Is., Niue, Samoa, Kiribati, New Caledonia,
        # Tuvalu, French Polynesia, Tokelau, Micronesia, Marshall Is.
    }
)

#: Caribbean countries inside +1 (they look domestic but bill international). Wangiri
#: "one ring" callbacks and lottery scams concentrate here.
IRSF_NANP_REGIONS = frozenset(
    {
        "AG", "AI", "BB", "BM", "BS", "DM", "DO", "GD", "JM", "KN", "KY", "LC", "MS",
        "SX", "TC", "TT", "VC", "VG",
    }
)

#: Number types that cost the caller a premium (and pay someone a share) in any country.
BLOCKED_NUMBER_TYPES = frozenset(
    {
        PhoneNumberType.PREMIUM_RATE,
        PhoneNumberType.SHARED_COST,
        PhoneNumberType.PERSONAL_NUMBER,
        PhoneNumberType.PAGER,
    }
)

#: US area codes outside the contiguous 48 that phonenumbers still labels "US".
NON_CONTIGUOUS_US_NPAS = frozenset({"907", "808"})


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def destination_region(e164: str) -> str | None:
    """Region key a number belongs to for this policy, or None when it cannot be placed.

    "US" means the contiguous 48; Alaska and Hawaii come back as "US-AK" / "US-HI" so a
    US workspace does not reach them by default.
    """
    try:
        number = phonenumbers.parse(e164 if e164.startswith("+") else "+" + e164, None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(number)
    if number.country_code == 1:
        if region is None:
            # Every Canadian, Caribbean and territory area code is in the metadata, so an
            # unrecognised +1 number is an unassigned US-style NPA (tests use 555). It
            # cannot reach a premium carrier; treat it as domestic.
            region = "US"
        if region == "US":
            npa = str(number.national_number)[:3]
            if npa == "907":
                return "US-AK"
            if npa == "808":
                return "US-HI"
    return region


def classify(e164: str) -> str | None:
    """Stateless half of the policy: BLOCKED for a number that is never allowed, else None."""
    if _digits(e164) in EMERGENCY_NUMBERS:
        return None
    try:
        number = phonenumbers.parse(e164 if e164.startswith("+") else "+" + e164, None)
    except phonenumbers.NumberParseException:
        return BLOCKED
    if number.country_code in NON_GEOGRAPHIC_COUNTRY_CODES:
        return BLOCKED
    if number.country_code in IRSF_COUNTRY_CODES:
        return BLOCKED
    region = phonenumbers.region_code_for_number(number)
    if number.country_code == 1 and region in IRSF_NANP_REGIONS:
        return BLOCKED
    if phonenumbers.number_type(number) in BLOCKED_NUMBER_TYPES:
        return BLOCKED
    return None


def decide(e164: str, home_region: str) -> str | None:
    """Pure policy: None when ``e164`` may be reached from a workspace in ``home_region``."""
    if _digits(e164) in EMERGENCY_NUMBERS:
        return None
    blocked = classify(e164)
    if blocked is not None:
        return blocked
    region = destination_region(e164)
    if region is None:
        return BLOCKED
    if region != (home_region or "US").upper():
        return NOT_ALLOWED
    return None


async def check(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, e164: str
) -> str | None:
    """Refusal code for this workspace reaching ``e164``, or None when allowed."""
    if not getattr(settings, "destination_policy_enforced", True):
        return None
    from app.services import phone_region

    home = await phone_region.for_org(session, org_id)
    return decide(e164, home)
