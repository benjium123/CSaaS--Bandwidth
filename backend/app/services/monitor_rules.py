"""P43: fixed scam rules under the AI - instant, free, and still working if the AI isn't.

Rules are deliberately conservative: they HOLD suspicious texts for the AI's second look and
only BLOCK combinations that are essentially never legitimate (e.g. "pay with gift cards" +
a government or bank impersonation). The AI makes the nuanced calls.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

URL_RE = re.compile(
    r"(?i)\b((?:https?://|www\.)[^\s<>\"']+|[a-z0-9-]+(?:\.[a-z0-9-]+)+/[^\s<>\"']*)"
)

LINK_SHORTENERS = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "is.gd",
        "ow.ly",
        "cutt.ly",
        "rebrand.ly",
        "shorturl.at",
        "rb.gy",
        "t.ly",
        "tiny.cc",
        "bl.ink",
        "s.id",
        "v.gd",
        "qrco.de",
        "lnkd.in",
        "buff.ly",
    }
)
RISKY_TLDS = frozenset(
    {
        "top",
        "xyz",
        "click",
        "zip",
        "mov",
        "icu",
        "buzz",
        "cam",
        "rest",
        "cfd",
        "sbs",
        "gq",
        "tk",
        "ml",
        "cf",
        "ga",
        "work",
        "support",
        "live",
        "shop",
    }
)

PAYMENT_TRAPS = re.compile(
    r"(?i)\b(gift ?cards?|itunes cards?|google play cards?|steam cards?|bitcoin|"
    r"crypto(currency)?|usdt|"
    r"wire (the )?(money|funds|payment)|western union|moneygram|zelle me|cash ?app me|"
    r"bitcoin atm|crypto atm)\b"
)
IMPERSONATION = re.compile(
    r"(?i)\b(irs|internal revenue service|social security (administration|office)|ssa\b|"
    r"medicare|u\.?s\.? ?customs|fbi|dea|homeland security|police department|sheriff|"
    r"hmrc|cra\b|canada revenue agency|service canada|"
    r"your bank|bank of america|chase bank|wells fargo|citibank|paypal|amazon security|"
    r"apple support|microsoft support|usps|royal mail|canada post|fedex|ups delivery|dhl)\b"
)
THREATS = re.compile(
    r"(?i)\b(arrest(ed)?|warrant|lawsuit|legal action|suspend(ed)? (your )?(account|ssn|social)|"
    r"account (is )?(locked|frozen|suspended|compromised)|deport(ed|ation)|"
    r"final (notice|warning)|immediate(ly)? action|within 24 hours|avoid (penalty|charges))\b"
)
CREDENTIAL_ASKS = re.compile(
    r"(?i)\b(verify your (identity|account|details|card)|confirm your (ssn|social security|"
    r"password|pin|card number|bank details)|"
    r"send (me|us) the (code|otp)|remote access|anydesk|teamviewer)\b"
)
PRIZE_BAIT = re.compile(
    r"(?i)\b(you('ve| have)? won|claim your (prize|reward|refund)|congratulations[,!]? you|"
    r"unclaimed (refund|funds|package)|you are (selected|eligible) for a (grant|refund))\b"
)


@dataclass
class RuleResult:
    action: str = "allow"  # allow | hold | block
    category: str = "none"
    reasons: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    candidate = url if "://" in url else f"http://{url}"
    try:
        return (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def extract_links(body: str) -> list[str]:
    return [m.group(1) for m in URL_RE.finditer(body or "")]


def evaluate(body: str, *, trusted_hosts: frozenset[str] = frozenset()) -> RuleResult:
    text = body or ""
    result = RuleResult(links=extract_links(text))
    hold: list[str] = []

    for link in result.links:
        host = _host(link)
        if not host or host in trusted_hosts or any(host.endswith("." + t) for t in trusted_hosts):
            continue
        if host.startswith("www."):
            host = host[4:]
        if host in LINK_SHORTENERS:
            hold.append(f"Uses a link shortener ({host}) that hides where it goes")
        try:
            ipaddress.ip_address(host)
            hold.append("Links straight to an IP address")
        except ValueError:
            pass
        if "xn--" in host:
            hold.append("Link uses a look-alike (punycode) domain")
        if host.rsplit(".", 1)[-1] in RISKY_TLDS:
            hold.append(f"Link on a domain type often used by scams ({host})")

    payment = bool(PAYMENT_TRAPS.search(text))
    impersonation = bool(IMPERSONATION.search(text))
    threat = bool(THREATS.search(text))
    credentials = bool(CREDENTIAL_ASKS.search(text))
    prize = bool(PRIZE_BAIT.search(text))

    if payment and (impersonation or threat):
        result.action = "block"
        result.category = "impersonation_payment"
        result.reasons = [
            "Asks for gift cards, crypto or wire transfers with a threat or an impersonation"
        ]
        return result
    if impersonation and threat and (credentials or result.links):
        result.action = "block"
        result.category = "phishing"
        result.reasons = [
            "Impersonates a government agency, bank or delivery company with a threat and a "
            "link or data request"
        ]
        return result

    if payment:
        hold.append("Mentions paying by gift cards, crypto or wire transfer")
    if credentials:
        hold.append("Asks for codes, passwords, card or identity details")
    if prize:
        hold.append("Prize, refund or grant bait")
    if impersonation and (threat or result.links):
        hold.append("Mentions a government agency, bank or delivery company with urgency or a link")

    if hold:
        result.action = "hold"
        result.category = "suspicious"
        result.reasons = hold
    return result
