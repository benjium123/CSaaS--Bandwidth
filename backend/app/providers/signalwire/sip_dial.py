"""The LaML answer we owe SignalWire for the SIP leg LiveKit opens.

Outbound CSaaS call: CSaaS -> LiveKit `CreateSIPParticipant` -> INVITE to our SignalWire Domain
Application (To = the number being called, From = our SignalWire number) -> SignalWire POSTs
this webhook -> we return a <Dial> -> SignalWire bridges to the PSTN with that number as the
caller id and the audio comes back over the same SIP leg.

Without an answer SignalWire replies 480 and the call dies, so this sits on the call path and
does exactly three things: verify, parse, render. No database, no outbound I/O.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

#: Both spellings, same as the voice webhooks: SignalWire sends its own header, Twilio sends
#: X-Twilio-Signature, and the Compatibility API has been observed doing either.
_SIGNATURE_HEADERS = ("x-twilio-signature", "x-signalwire-signature")

#: The one URL SignalWire signs for this callback. Named rather than inlined because the
#: deployment's public base url is the only thing that varies between environments.
_DIAL_PATH = "/api/v1/webhooks/signalwire/sip-dial"

#: `&quot;` for the callerId attribute (quoteattr would do it too, but it also picks its own
#: quote character, and the document shape here is fixed by SignalWire's parser).
_ATTR_ESCAPES = {'"': "&quot;"}

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def is_e164(value: str) -> bool:
    """SignalWire hands us whatever the INVITE carried; a malformed number would otherwise be
    billed as a call to nowhere."""
    return bool(E164.match(value))


def signing_url(public_base_url: str) -> str:
    if not public_base_url:
        return ""
    return public_base_url.rstrip("/") + _DIAL_PATH


def _signature_from(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() in _SIGNATURE_HEADERS:
            return value
    return ""


def verify(
    headers: Mapping[str, str], raw_body: bytes, signing_url: str, api_token: str
) -> bool:
    if not signing_url:
        # Fail CLOSED, same reasoning as verify_voice_webhook: with nothing to sign against
        # there is no way to tell a real callback from anyone who knows the webhook path, and
        # guessing "true" is how a webhook becomes an open door to our phone bill.
        logger.warning("signalwire_sip_dial_signature_unverifiable")
        return False
    if not api_token:
        logger.warning("signalwire_sip_dial_token_missing")
        return False

    signature = _signature_from(headers)
    if not signature:
        logger.warning("signalwire_sip_dial_signature_missing")
        return False

    params = parse_qsl(raw_body.decode("utf-8", "replace"), keep_blank_values=True)
    params.sort(key=lambda pair: pair[0])
    # Twilio's scheme: the URL, then each POST param as name immediately followed by its
    # value, in name order, with nothing at all between one pair and the next.
    payload = signing_url + "".join(name + value for name, value in params)
    digest = hmac.new(api_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    # compare_digest, not ==: a short-circuiting comparison leaks how much of a guess was
    # right. Neither the signature nor the token is ever logged.
    if not hmac.compare_digest(expected, signature):
        logger.warning("signalwire_sip_dial_signature_mismatch")
        return False
    return True


def diagnose(
    headers: Mapping[str, str], raw_body: bytes, public_base_url: str, api_token: str
) -> str:
    """Which (header, url spelling, hash) combination SignalWire's signature matches, if any.

    Only ever called on the refused path. Returns a label, never a signature or the token;
    "none" means the key SignalWire signed with is not the API token we hold.
    """
    if not (public_base_url and api_token):
        return "unconfigured"
    base = signing_url(public_base_url)
    urls = {
        "https": base,
        "https_slash": base + "/",
        "http": base.replace("https://", "http://", 1),
        "https_443": base.replace("://", "://", 1).replace(
            base.split("/")[2], base.split("/")[2] + ":443", 1
        ),
    }
    params = parse_qsl(raw_body.decode("utf-8", "replace"), keep_blank_values=True)
    ordered = "".join(name + value for name, value in sorted(params, key=lambda p: p[0]))
    unordered = "".join(name + value for name, value in params)
    bodies = {"sorted": ordered, "asis": unordered, "rawbody": raw_body.decode("utf-8", "replace")}
    for header, received in headers.items():
        if "signature" not in header.lower():
            continue
        for url_label, url in urls.items():
            for body_label, body in bodies.items():
                for algo_label, algo in (("sha1", hashlib.sha1), ("sha256", hashlib.sha256)):
                    mac = hmac.new(api_token.encode(), (url + body).encode(), algo)
                    candidates = (base64.b64encode(mac.digest()).decode(), mac.hexdigest())
                    if any(hmac.compare_digest(c, received) for c in candidates):
                        return f"{header.lower()}:{url_label}:{body_label}:{algo_label}"
    return "none"


def build_laml(to_e164: str, caller_id: str) -> str:
    """answerOnBridge is load-bearing: without it SignalWire answers our SIP leg the moment it
    arrives and the caller hears silence instead of ringback while the PSTN leg is dialled."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial answerOnBridge="true" callerId="{escape(caller_id, _ATTR_ESCAPES)}">'
        f"<Number>{escape(to_e164)}</Number>"
        "</Dial>"
        "</Response>"
    )
