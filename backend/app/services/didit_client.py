"""Didit adapter for identity verification (document + selfie checks).

This module is the provider seam for KYC: the rest of the app talks to the small
surface below (create a session, verify a webhook, map a status to an internal
outcome) and never to Didit's HTTP API directly. That keeps a future provider swap
or a second provider behind one file, and it keeps Didit's exact wire format -
which is easy to get subtly wrong - in one place.

Two things here are security-critical and deliberately explicit:

* Webhook authenticity. Didit signs the payload, not just the envelope. We verify
  the V2 signature over a canonical re-serialisation of the parsed JSON, and we
  reject ``X-Signature-Simple`` outright (see ``verify_webhook``).
* Replay protection. A valid signature is not enough on its own; the timestamp
  must be recent, and that check runs even when the signature verifies.

Secrets and document data are never logged. Only non-identifying facts (session
id, status, event id) reach the logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from app.errors import FeatureUnavailableError, UnauthenticatedError

log = structlog.get_logger("didit_client")

#: How far a webhook timestamp may drift from our clock before we treat it as a
#: replay. Didit's own guidance is 300 seconds; anything older is refused.
REPLAY_WINDOW_SECONDS = 300

#: Customer-safe message reused everywhere the provider is not usable. It never
#: names Didit or leaks configuration detail.
_NOT_SET_UP = "Identity verification is not set up yet."

#: Customer-safe message for a webhook we could not authenticate.
_NOT_VERIFIED = "We could not verify that this came from our identity provider."


# ------------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------------
def _secret_value(configured: Any) -> str:
    """Read a SecretStr (or plain string) without ever logging it.

    ``get_secret_value`` is the only way to unwrap a SecretStr; a plain string is
    accepted too so tests can pass a lightweight settings stub.
    """
    if configured is None:
        return ""
    try:
        value = configured.get_secret_value()
    except AttributeError:
        value = configured
    except Exception:
        return ""
    return (value or "").strip()


def is_configured(settings) -> bool:
    """True only when both the API key and the workflow id are present.

    A session cannot be created without a workflow id, so a key alone is not
    enough to call the provider usable.
    """
    api_key = _secret_value(getattr(settings, "didit_api_key", None))
    workflow_id = (getattr(settings, "didit_workflow_id", "") or "").strip()
    return bool(api_key and workflow_id)


# ------------------------------------------------------------------------------------
# Session creation
# ------------------------------------------------------------------------------------
async def create_session(
    settings,
    *,
    vendor_data: str,
    metadata: dict,
    callback: str | None = None,
    contact_email: str | None = None,
    language: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Create a Didit verification session and return where to send the user.

    ``vendor_data`` carries our KycPerson id: Didit uses it for duplicate detection,
    so it must be stable across retries for the same person. ``metadata`` is echoed
    back on webhooks and is how we correlate a callback with our own records.

    Returns ``{"session_id", "url", "status"}``. ``url`` is the redirect target -
    Didit names the field ``url``, not ``verification_url``.
    """
    if not is_configured(settings):
        raise FeatureUnavailableError(_NOT_SET_UP)

    api_key = _secret_value(getattr(settings, "didit_api_key", None))
    workflow_id = (getattr(settings, "didit_workflow_id", "") or "").strip()
    base_url = (getattr(settings, "didit_base_url", "") or "").rstrip("/")
    endpoint = f"{base_url}/v3/session/"

    body: dict[str, Any] = {
        "workflow_id": workflow_id,
        "vendor_data": vendor_data,
        "metadata": metadata,
    }
    # Optional fields are only sent when we actually have a value: sending an empty
    # string would be a different request than omitting the key.
    if callback:
        body["callback"] = callback
    if contact_email:
        body["contact_details"] = {"email": contact_email}
    if language:
        body["language"] = language

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    async def _post(http_client: httpx.AsyncClient) -> httpx.Response:
        return await http_client.post(endpoint, json=body, headers=headers)

    try:
        if client is not None:
            response = await _post(client)
        else:
            async with httpx.AsyncClient(timeout=20.0) as owned_client:
                response = await _post(owned_client)
    except httpx.HTTPError as exc:
        # The exception text can include the request URL but never the body; we still
        # keep the log to the error type so nothing user-shaped can leak.
        log.warning("didit_session_request_failed", error=type(exc).__name__)
        raise FeatureUnavailableError(
            "We could not start identity verification right now. Please try again."
        ) from exc

    if response.status_code < 200 or response.status_code >= 300:
        # Log the status code only. The response body can echo the submitted data.
        log.warning("didit_session_rejected", status_code=response.status_code)
        raise FeatureUnavailableError(
            "We could not start identity verification right now. Please try again."
        )

    try:
        data = response.json()
    except ValueError as exc:
        log.warning("didit_session_bad_json", status_code=response.status_code)
        raise FeatureUnavailableError(
            "We could not start identity verification right now. Please try again."
        ) from exc

    # A well-formed JSON body that is not an object (array, string, number, null) has
    # no ``.get``; treat it as a provider failure rather than letting AttributeError
    # escape as a 500.
    if not isinstance(data, dict):
        log.warning("didit_session_unexpected_shape", status_code=response.status_code)
        raise FeatureUnavailableError(
            "We could not start identity verification right now. Please try again."
        )

    session_id = data.get("session_id")
    url = data.get("url")
    if not session_id or not url:
        # A session we cannot redirect to is useless, so treat it as a failure rather
        # than handing the caller a half-built result.
        log.warning("didit_session_incomplete", status_code=response.status_code)
        raise FeatureUnavailableError(
            "We could not start identity verification right now. Please try again."
        )

    log.info("didit_session_created", session_id=str(session_id), status=data.get("status"))
    return {
        "session_id": str(session_id),
        "url": str(url),
        "status": data.get("status"),
    }


# ------------------------------------------------------------------------------------
# Webhook verification
# ------------------------------------------------------------------------------------
def _normalise_json_numbers(value: Any) -> Any:
    """Recursively normalise whole-valued floats to ints for canonical signing.

    Didit's V2 canonical form is the JSON re-serialisation of the parsed payload
    with whole-valued numbers rendered as integers (``1`` not ``1.0``). Python's
    ``json.loads`` turns ``1`` into ``int`` and ``1.0`` into ``float``, so a payload
    that arrived as ``1.0`` would otherwise canonicalise differently from the same
    payload that arrived as ``1`` and the signature would not match.

    ``bool`` is a subclass of ``int`` in Python and must be preserved as-is (``True``
    is not ``1``). ``None`` and non-integral floats are preserved. Negative zero
    (``-0.0``) is normalised to ``0`` so it matches the integer form.

    Raises ``ValueError`` on non-finite numbers (NaN/Infinity), which strict JSON
    cannot represent. The input is never mutated: containers are rebuilt.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            # NaN/Infinity cannot be represented in strict JSON; the caller turns this
            # into an authentication failure rather than emitting invalid JSON.
            raise ValueError("non-finite number in payload")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {key: _normalise_json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise_json_numbers(item) for item in value]
    return value


def canonical_payload(payload: dict) -> bytes:
    """The exact bytes Didit signs for ``X-Signature-V2``.

    Didit re-serialises the parsed JSON with sorted keys and no whitespace before
    signing, so we must reproduce that byte-for-byte. Whole-valued floats are
    normalised to ints recursively (including inside nested lists/dicts) so a
    payload that arrived as ``1.0`` canonicalises identically to one that arrived
    as ``1``. The input payload is not mutated.

    Raises ``ValueError`` if the payload contains a non-finite number (NaN/Infinity)
    or cannot be canonically encoded (including unpaired surrogates, which raise
    ``UnicodeEncodeError`` - a ``ValueError`` subclass); ``verify_webhook`` converts
    that into an ``UnauthenticatedError`` so a malformed payload never surfaces as a
    500.
    """
    normalised = _normalise_json_numbers(payload)
    return json.dumps(
        normalised,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that also works for a plain dict.

    FastAPI hands us a ``Headers`` mapping which is already case-insensitive, but
    tests and internal callers may pass a plain dict, so we normalise here.
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _signature_matches(secret: str, signed_bytes: bytes, provided: str) -> bool:
    """Constant-time comparison of hex digests, lowercased on both sides."""
    expected = hmac.new(secret.encode("utf-8"), signed_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), (provided or "").strip().lower())


def verify_webhook(settings, raw_body: bytes, headers: Mapping[str, str]) -> dict:
    """Authenticate a Didit webhook and return the parsed payload.

    Raises ``FeatureUnavailableError`` when no webhook secret is configured and
    ``UnauthenticatedError`` for anything we cannot prove came from Didit.
    """
    secret = _secret_value(getattr(settings, "didit_webhook_secret", None))
    if not secret:
        raise FeatureUnavailableError(_NOT_SET_UP)

    try:
        payload = json.loads(raw_body)
        # Reject non-finite numbers (NaN/Infinity) at parse time so BOTH the V2
        # canonical path and the legacy raw-body X-Signature path fail closed on a
        # payload that strict JSON cannot represent. The normalised copy is discarded:
        # the raw body bytes are still what the legacy signature is checked against.
        _normalise_json_numbers(payload)
    except (ValueError, TypeError) as exc:
        log.warning("didit_webhook_malformed_json")
        raise UnauthenticatedError(_NOT_VERIFIED) from exc

    if not isinstance(payload, dict):
        # A JSON array or scalar is not a webhook we can act on.
        log.warning("didit_webhook_unexpected_shape")
        raise UnauthenticatedError(_NOT_VERIFIED)

    timestamp_raw = _header(headers, "X-Timestamp")
    try:
        timestamp = int(str(timestamp_raw).strip())
    except (TypeError, ValueError) as exc:
        log.warning("didit_webhook_missing_timestamp")
        raise UnauthenticatedError(_NOT_VERIFIED) from exc

    # Replay protection runs BEFORE the signature check and regardless of its result:
    # a captured, perfectly-signed request must still expire.
    if abs(time.time() - timestamp) > REPLAY_WINDOW_SECONDS:
        log.warning("didit_webhook_stale_timestamp")
        raise UnauthenticatedError(_NOT_VERIFIED)

    signature_v2 = _header(headers, "X-Signature-V2")
    signature_v1 = _header(headers, "X-Signature")

    # ``X-Signature-Simple`` is deliberately never consulted. It authenticates only the
    # envelope, not the decision inside it, so an attacker who captured one could swap a
    # Declined decision for an Approved one and still pass verification. Its mere
    # presence must never authenticate a request.
    if signature_v2:
        try:
            signed_bytes = canonical_payload(payload)
        except (ValueError, TypeError) as exc:
            # A payload we cannot canonically encode (non-finite number, unpaired
            # surrogate, unexpected type) is not one we can authenticate; fail closed
            # rather than 500.
            log.warning("didit_webhook_uncanonicalisable")
            raise UnauthenticatedError(_NOT_VERIFIED) from exc
        ok = _signature_matches(secret, signed_bytes, signature_v2)
    elif signature_v1:
        # The legacy header signs the raw body bytes as received.
        ok = _signature_matches(secret, raw_body, signature_v1)
    else:
        ok = False

    if not ok:
        log.warning("didit_webhook_signature_mismatch")
        raise UnauthenticatedError(_NOT_VERIFIED)

    return payload


# ------------------------------------------------------------------------------------
# Status mapping
# ------------------------------------------------------------------------------------
#: Didit session status -> the internal outcome status our KYC service already understands.
#: Keys are EXACT and case-sensitive; an unknown status maps to nothing (see below).
#:
#: Reasoning for the non-obvious rows:
#: * "Declined" -> "requires_input": a decline is usually a bad photo or a mismatch the
#:   user can retry, so we ask for more input rather than treating it as a hard failure.
#: * "Resubmitted" -> "requires_input": Didit uses this status when the reviewer has
#:   asked the user to redo steps. The frontend only renders the retry affordance for
#:   "requires_input", so mapping it to "processing" would hide the retry the user
#:   actually needs.
#: * "In Review"/"In Progress" -> "processing": both mean Didit is still working, so
#:   the person must not be moved to a terminal state yet.
#: * "Awaiting User" -> "pending": the ball is in the user's court, not ours.
#: * "Abandoned"/"Expired"/"Kyc Expired" -> "canceled": the session is over without a
#:   decision, so the person is left unverified and can start again.
#: * "Not Started" -> None: nothing has happened yet, so there is nothing to record.
STATUS_MAP: dict[str, str | None] = {
    "Approved": "verified",
    "Declined": "requires_input",
    "In Review": "processing",
    "In Progress": "processing",
    "Resubmitted": "requires_input",
    "Awaiting User": "pending",
    "Abandoned": "canceled",
    "Expired": "canceled",
    "Kyc Expired": "canceled",
    "Not Started": None,
}

#: Safe, non-provider error codes surfaced to the caller for statuses that need one.
#: These are OUR strings, never raw provider reasons, so nothing Didit-specific leaks.
ERROR_CODE_MAP: dict[str, str] = {
    "Declined": "verification_declined",
    "Resubmitted": "verification_resubmission_required",
}


def _id_verification(payload: dict) -> dict:
    """The first ``decision.id_verifications`` entry, or an empty dict.

    Didit reports every feature as a PLURAL ARRAY (``id_verifications``, never
    ``id_verification``) because a workflow can run the same feature more than once.
    The first element is the one the decision is based on.

    Every layer here is defensive on purpose: ``decision`` is absent on non-terminal
    statuses, the array can be missing or EMPTY, and an element can be a non-mapping.
    A webhook that raises is worse than one that yields no identity, because the
    caller already fails closed on missing identity.
    """
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        return {}
    reports = decision.get("id_verifications")
    if not isinstance(reports, list) or not reports:
        return {}
    first = reports[0]
    return first if isinstance(first, dict) else {}


def _text(value: Any) -> str | None:
    """A trimmed non-empty string, or None. Numbers and nulls become None."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


#: ``YYYY-MM-DD`` possibly followed by a time component.
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _normalise_dob(value: Any) -> str | None:
    """Return the date of birth as ``YYYY-MM-DD``, the format Stripe's path produces.

    ``kyc.identity_hash`` hashes the dob string verbatim, so two spellings of the same
    birthday hash to two different values and the same human reads as a stranger on
    re-verification. Stripe's adapter builds ``f"{year:04d}-{month:02d}-{day:02d}"``;
    anything we cannot confidently normalise to that becomes None rather than a
    differently-shaped string that would poison the hash.
    """
    text = _text(value)
    if text is None:
        return None
    iso = _ISO_DATE.match(text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    # A slash- or dash-separated date like "03/04/1990" is DELIBERATELY not parsed.
    # It is ambiguous - day-first across most of the world, month-first in the US - and
    # Didit documents ISO, so any such value is already off the documented path. There is
    # no evidence in the payload saying which order it is in, so parsing it is a coin
    # flip, and the losing side of that flip does not surface as an error. It writes a
    # hash for the wrong birthday, silently, and then months later a real customer fails
    # re-verification with `identity_mismatch` and a security alert raised against
    # them - and the evidence needed to diagnose it was discarded at this line.
    # Returning None parks them for a human instead, which is exactly what we already do
    # with every other payload we cannot read.
    return None


def outcome_from_payload(payload: dict) -> dict | None:
    """Translate a verified webhook payload into the shape the KYC service consumes.

    Returns ``None`` for an unmapped, unknown or missing status: the caller then does
    nothing. It must not crash and must not change the person on a status we do not
    recognise.

    Identity comes from ``decision.id_verifications[0]``. Didit's ``issuing_state`` is
    the issuing COUNTRY of the document and maps onto our ``document_country``. When a
    payload carries no usable identity every identity field is None and the KYC service
    fails closed on it - missing data must never raise.
    """
    status = payload.get("status")
    internal = STATUS_MAP.get(status) if isinstance(status, str) else None
    if internal is None:
        return None

    report = _id_verification(payload)
    first_name = _text(report.get("first_name"))
    last_name = _text(report.get("last_name"))
    full_name = _text(report.get("full_name"))
    if not first_name and not last_name and full_name:
        # Some documents give only a single full name field. ``identity_hash`` hashes a
        # SORTED word set of "first last", so handing the whole name through as the
        # first name produces exactly the hash a split name would have produced.
        first_name = full_name

    # ``metadata`` is echoed back from our own request, but a signed payload could still
    # carry a non-dict (list, string, null). Coerce to an empty dict so a malformed
    # signed payload cannot raise here.
    raw_metadata = payload.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}

    # A safe, non-provider error code for statuses that need one. Never a raw Didit
    # reason: those can carry document data and are not stable across versions.
    error_code = ERROR_CODE_MAP.get(status) if isinstance(status, str) else None

    return {
        "session_id": payload.get("session_id"),
        "status": internal,
        "metadata": metadata,
        "vendor_data": payload.get("vendor_data"),
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "dob": _normalise_dob(report.get("date_of_birth")),
        "document_type": _text(report.get("document_type")),
        "document_number": _text(report.get("document_number")),
        # Didit names the issuing country "issuing_state".
        "document_country": _text(report.get("issuing_state")),
        "error_code": error_code,
    }
