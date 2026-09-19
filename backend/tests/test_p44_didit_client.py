"""Tests for the Didit adapter (app/services/didit_client.py).

Covered here:

* ``create_session`` - the exact documented request (URL, headers, body), omission of
  optional fields, refusal when unconfigured (before any I/O), and failure handling for
  a non-2xx response or a response missing the redirect ``url``.
* ``verify_webhook`` - V2 signature over the canonical re-serialisation, tampered-body
  rejection, corrupt signatures, the absolute replay window, refusal of
  ``X-Signature-Simple``, the legacy ``X-Signature`` over the raw body, the
  unconfigured-provider case, and malformed JSON.
* ``outcome_from_payload`` / ``STATUS_MAP`` - every documented status, unknown/missing/
  non-string statuses, case sensitivity, and the deliberately-None identity fields.

No network calls are made anywhere: every HTTP interaction goes through an
``httpx.MockTransport``. Note that httpx emits an identical ``HTTP Request:`` log line for
mocked and real transports, so log output is never used as evidence here - assertions are
made against the captured ``httpx.Request`` object.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.errors import FeatureUnavailableError, UnauthenticatedError
from app.services import didit_client
from tests.conftest import make_settings

DIDIT_KEY = "didit-test-api-key"
DIDIT_SECRET = "didit-test-webhook-secret"
WORKFLOW_ID = "11111111-1111-1111-1111-111111111111"

PERSON_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
RETURN_URL = "https://app.example.test/kyc/return"


# ------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------
def didit_settings(**overrides):
    # Built as one dict so a test can override a single Didit setting (blank the key, blank
    # the secret) without colliding with the defaults below.
    base = {
        "kyc_identity_provider": "didit",
        "didit_api_key": DIDIT_KEY,
        "didit_workflow_id": WORKFLOW_ID,
        "didit_webhook_secret": DIDIT_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def signed_headers(
    payload: dict,
    *,
    secret: str = DIDIT_SECRET,
    timestamp: int | None = None,
    header: str = "X-Signature-V2",
) -> tuple[bytes, dict[str, str]]:
    """Returns (raw_body, headers) for a payload signed the way Didit signs it.

    ``raw_body`` is deliberately the plain ``json.dumps`` form, NOT the canonical form.
    That way the tests prove the V2 signature is computed over the re-serialised payload
    rather than over whatever bytes happened to arrive on the wire.
    """
    raw_body = json.dumps(payload).encode()
    if timestamp is None:
        timestamp = int(time.time())

    if header == "X-Signature-V2":
        signed_bytes = didit_client.canonical_payload(payload)
    else:
        # The legacy header signs the raw body bytes as received.
        signed_bytes = raw_body

    signature = hmac.new(secret.encode(), signed_bytes, hashlib.sha256).hexdigest()
    return raw_body, {"X-Timestamp": str(timestamp), header: signature}


def webhook_payload(
    *,
    status: str = "Approved",
    session_id: str = "sess-1",
    event_id: str = "evt-1",
    org_id: str = ORG_ID,
    person_id: str = PERSON_ID,
) -> dict:
    """A representative Didit webhook payload.

    Terminal statuses carry a ``decision`` sub-object, mirroring what Didit sends once a
    session has concluded.
    """
    payload: dict = {
        "webhook_type": "status.updated",
        "timestamp": int(time.time()),
        "created_at": "2024-01-01T00:00:00Z",
        "status": status,
        "session_id": session_id,
        "workflow_id": WORKFLOW_ID,
        "vendor_data": person_id,
        "metadata": {
            "purpose": "kyc_person",
            "org_id": org_id,
            "person_id": person_id,
        },
        "event_id": event_id,
    }
    if status in {"Approved", "Declined"}:
        payload["decision"] = {
            "status": status,
            "session_id": session_id,
        }
    return payload


def _capturing_transport(handler):
    """Wrap a handler so the test can inspect the captured request afterwards."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.MockTransport(_handler), captured


# ------------------------------------------------------------------------------------
# create_session
# ------------------------------------------------------------------------------------
async def test_create_session_sends_documented_request():
    """The request must match Didit's documented v3 session endpoint exactly."""
    metadata = {"purpose": "kyc_person", "org_id": ORG_ID, "person_id": PERSON_ID}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "session_id": "sess-abc",
                "url": "https://verify.didit.me/s/abc",
                "status": "Not Started",
                "vendor_data": PERSON_ID,
                "metadata": metadata,
            },
        )

    transport, captured = _capturing_transport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await didit_client.create_session(
            didit_settings(),
            vendor_data=PERSON_ID,
            metadata=metadata,
            callback=RETURN_URL,
            contact_email="owner@example.test",
            client=client,
        )

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://verification.didit.me/v3/session/"
    assert request.headers["x-api-key"] == DIDIT_KEY
    assert request.headers["content-type"].startswith("application/json")

    body = json.loads(request.content)
    assert body["workflow_id"] == WORKFLOW_ID
    assert body["vendor_data"] == PERSON_ID
    assert body["metadata"] == metadata
    assert body["callback"] == RETURN_URL
    assert body["contact_details"] == {"email": "owner@example.test"}

    # The redirect field Didit returns is named ``url``, not ``verification_url`` - that
    # is the easy mistake this assertion guards against.
    assert result == {
        "session_id": "sess-abc",
        "url": "https://verify.didit.me/s/abc",
        "status": "Not Started",
    }


async def test_create_session_omits_optional_fields_when_absent():
    """Optional fields must be omitted entirely, not sent as empty strings."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "session_id": "sess-abc",
                "url": "https://verify.didit.me/s/abc",
                "status": "Not Started",
            },
        )

    transport, captured = _capturing_transport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await didit_client.create_session(
            didit_settings(),
            vendor_data=PERSON_ID,
            metadata={"purpose": "kyc_person"},
            client=client,
        )

    body = json.loads(captured[0].content)
    assert "callback" not in body
    assert "contact_details" not in body
    assert "language" not in body


async def test_create_session_refuses_when_api_key_missing():
    """An unconfigured provider must refuse before any I/O happens."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made when unconfigured")

    transport, captured = _capturing_transport(handler)
    settings = didit_settings(didit_api_key="")
    assert didit_client.is_configured(settings) is False

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FeatureUnavailableError):
            await didit_client.create_session(
                settings,
                vendor_data=PERSON_ID,
                metadata={},
                client=client,
            )

    assert captured == []


async def test_create_session_refuses_when_workflow_id_missing():
    """A key alone is not enough: a session cannot be created without a workflow id."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made when unconfigured")

    transport, captured = _capturing_transport(handler)
    settings = didit_settings(didit_workflow_id="")
    assert didit_client.is_configured(settings) is False

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FeatureUnavailableError):
            await didit_client.create_session(
                settings,
                vendor_data=PERSON_ID,
                metadata={},
                client=client,
            )

    assert captured == []


async def test_is_configured_true_for_good_settings():
    assert didit_client.is_configured(didit_settings()) is True


async def test_create_session_raises_on_server_error():
    """A 500 from Didit is a provider outage, not a client error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    transport, _ = _capturing_transport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FeatureUnavailableError):
            await didit_client.create_session(
                didit_settings(),
                vendor_data=PERSON_ID,
                metadata={},
                client=client,
            )


async def test_create_session_raises_when_url_missing():
    """A session we cannot redirect to is useless, so it must be treated as a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"session_id": "sess-abc", "status": "Not Started"})

    transport, _ = _capturing_transport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FeatureUnavailableError):
            await didit_client.create_session(
                didit_settings(),
                vendor_data=PERSON_ID,
                metadata={},
                client=client,
            )


# ------------------------------------------------------------------------------------
# verify_webhook
# ------------------------------------------------------------------------------------
def test_verify_webhook_accepts_valid_v2_signature():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload)

    result = didit_client.verify_webhook(didit_settings(), raw_body, headers)

    assert result == payload


def test_verify_webhook_rejects_tampered_body():
    """The signature must cover the decision, not just the envelope.

    ``X-Signature-Simple`` authenticates only the envelope, so an attacker who captured a
    signed request could swap a Declined decision for an Approved one and still pass. This
    test is what proves we authenticate the decision itself: the signature header is left
    untouched while the body's status is changed.
    """
    payload = webhook_payload(status="Approved")
    raw_body, headers = signed_headers(payload)

    tampered = json.loads(raw_body)
    tampered["status"] = "Declined"
    tampered_body = json.dumps(tampered).encode()

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), tampered_body, headers)


def test_verify_webhook_rejects_corrupt_signature():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload)

    signature = headers["X-Signature-V2"]
    # Flip one hex character so the digest is well-formed but wrong.
    flipped = "0" if signature[0] != "0" else "1"
    headers["X-Signature-V2"] = flipped + signature[1:]

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_rejects_stale_timestamp():
    """A captured, perfectly-signed request must still expire."""
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload, timestamp=int(time.time()) - 400)

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)

    # The identical payload with a fresh timestamp verifies fine, so the failure above
    # cannot be attributed to a bad signature.
    fresh_body, fresh_headers = signed_headers(payload)
    assert didit_client.verify_webhook(didit_settings(), fresh_body, fresh_headers) == payload


def test_verify_webhook_rejects_future_timestamp():
    """The replay window is absolute: a clock in the future is refused too."""
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload, timestamp=int(time.time()) + 400)

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_rejects_missing_timestamp():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload)
    del headers["X-Timestamp"]

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_rejects_non_numeric_timestamp():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload)
    headers["X-Timestamp"] = "not-a-number"

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_never_accepts_simple_signature_over_raw_body():
    """``X-Signature-Simple`` must never authenticate a request, even when correct."""
    payload = webhook_payload()
    raw_body = json.dumps(payload).encode()
    simple = hmac.new(DIDIT_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-Simple": simple}

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_never_accepts_simple_signature_over_canonical_payload():
    """Same rule, but with the simple signature computed over the canonical payload."""
    payload = webhook_payload()
    raw_body = json.dumps(payload).encode()
    simple = hmac.new(
        DIDIT_SECRET.encode(), didit_client.canonical_payload(payload), hashlib.sha256
    ).hexdigest()
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-Simple": simple}

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_accepts_legacy_signature_over_raw_body():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload, header="X-Signature")

    assert didit_client.verify_webhook(didit_settings(), raw_body, headers) == payload


def test_verify_webhook_rejects_legacy_signature_of_wrong_bytes():
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload, header="X-Signature")
    # Sign different bytes with the same secret: the header is well-formed but wrong.
    headers["X-Signature"] = hmac.new(
        DIDIT_SECRET.encode(), b"some other body", hashlib.sha256
    ).hexdigest()

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), raw_body, headers)


def test_verify_webhook_unconfigured_secret_is_feature_unavailable():
    """An unconfigured provider is an operator problem, not a forged request."""
    payload = webhook_payload()
    raw_body, headers = signed_headers(payload)

    with pytest.raises(FeatureUnavailableError):
        didit_client.verify_webhook(didit_settings(didit_webhook_secret=""), raw_body, headers)


def test_verify_webhook_rejects_malformed_json():
    headers = {"X-Timestamp": str(int(time.time())), "X-Signature-V2": "deadbeef"}

    with pytest.raises(UnauthenticatedError):
        didit_client.verify_webhook(didit_settings(), b"{not json", headers)


# ------------------------------------------------------------------------------------
# outcome_from_payload / STATUS_MAP
# ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Approved", "verified"),
        ("Declined", "requires_input"),
        ("In Review", "processing"),
        ("In Progress", "processing"),
        ("Resubmitted", "processing"),
        ("Awaiting User", "pending"),
        ("Abandoned", "canceled"),
        ("Expired", "canceled"),
        ("Kyc Expired", "canceled"),
        ("Not Started", None),
    ],
)
def test_outcome_from_payload_maps_documented_statuses(status, expected):
    payload = webhook_payload(status=status)

    result = didit_client.outcome_from_payload(payload)

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result["status"] == expected


@pytest.mark.parametrize("status", ["Wibble", 42])
def test_outcome_from_payload_unknown_status_returns_none(status):
    payload = webhook_payload()
    payload["status"] = status

    assert didit_client.outcome_from_payload(payload) is None


def test_outcome_from_payload_missing_status_returns_none():
    payload = webhook_payload()
    del payload["status"]

    assert didit_client.outcome_from_payload(payload) is None


def test_outcome_from_payload_is_case_sensitive():
    """Didit's values are exact; silently accepting a different casing would be guessing."""
    payload = webhook_payload(status="approved")

    assert didit_client.outcome_from_payload(payload) is None


def test_outcome_from_payload_carries_metadata_and_vendor_data():
    payload = webhook_payload(status="Approved")

    result = didit_client.outcome_from_payload(payload)

    assert result is not None
    assert result["metadata"] == payload["metadata"]
    assert result["vendor_data"] == payload["vendor_data"]
    # This payload's ``decision`` carries no ``id_verifications`` array, so there is no
    # identity to map and every identity field stays None. The KYC service handles the
    # all-None case explicitly (it fails closed on re-verification). The mapping itself
    # is covered below, from ``decision.id_verifications[0]``.
    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["dob"] is None
    assert result["document_type"] is None
    assert result["document_country"] is None
    assert result["document_number"] is None
    assert result["full_name"] is None
    assert result["error_code"] is None


# ==================================================================================
# Identity mapping: decision.id_verifications[0] -> outcome fields
# ==================================================================================


def approved_with_identity(**overrides):
    """Build an Approved Didit payload with a realistic id_verifications[0] record.

    The recording keys mirror Didit's plural array. ``overrides`` replace keys in
    that record, so a test can blank a name or supply a malformed date without
    hand-building a payload.
    """
    payload = webhook_payload(status="Approved")
    record = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "full_name": "Ada Lovelace",
        "date_of_birth": "1815-12-10",
        "document_type": "Passport",
        "document_number": "P1234567",
        "issuing_state": "GB",
    }
    record.update(overrides)
    payload["decision"]["id_verifications"] = [record]
    return payload


def test_outcome_from_payload_full_approved_identity_maps_all_fields():
    """Full Approved payload maps every identity field and still reports verified."""
    result = didit_client.outcome_from_payload(approved_with_identity())

    assert result is not None
    assert result["status"] == "verified"
    assert result["first_name"] == "Ada"
    assert result["last_name"] == "Lovelace"
    assert result["full_name"] == "Ada Lovelace"
    assert result["dob"] == "1815-12-10"
    assert result["document_type"] == "Passport"
    assert result["document_number"] == "P1234567"
    assert result["document_country"] == "GB"
    assert result["error_code"] is None


def test_outcome_from_payload_missing_decision_yields_none_identity_fields():
    """A payload without decision must not raise, and every identity field is None."""
    payload = webhook_payload(status="Approved")
    del payload["decision"]

    result = didit_client.outcome_from_payload(payload)

    assert result is not None
    assert result["status"] == "verified"
    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["dob"] is None
    assert result["document_type"] is None
    assert result["document_number"] is None
    assert result["document_country"] is None
    assert result["error_code"] is None


def test_outcome_from_payload_missing_id_verifications_yields_none_identity_fields():
    """A decision object without id_verifications must fail closed, not raise."""
    payload = approved_with_identity()
    del payload["decision"]["id_verifications"]

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["dob"] is None
    assert result["document_type"] is None
    assert result["document_number"] is None
    assert result["document_country"] is None


def test_outcome_from_payload_empty_id_verifications_list_yields_none_identity_fields():
    """An EMPTY id_verifications list is the case most likely to throw; it must not."""
    payload = approved_with_identity()
    payload["decision"]["id_verifications"] = []

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["dob"] is None
    assert result["document_type"] is None
    assert result["document_number"] is None
    assert result["document_country"] is None


def test_outcome_from_payload_id_verifications_not_a_list_yields_none_identity_fields():
    """A singular id_verification dict (the shape we must NOT accept) yields None."""
    payload = approved_with_identity()
    payload["decision"]["id_verifications"] = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "date_of_birth": "1815-12-10",
    }

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["dob"] is None


def test_outcome_from_payload_first_id_verification_not_a_dict_yields_none_identity_fields():
    """Even when the list exists, a non-dict element must yield None identity fields."""
    payload = approved_with_identity()
    payload["decision"]["id_verifications"] = ["not-a-dict"]

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] is None
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["dob"] is None


def test_outcome_from_payload_individual_missing_keys_map_only_what_exists():
    """A partial record maps only its present keys; the full record proves direction."""
    full = didit_client.outcome_from_payload(approved_with_identity())
    assert full["first_name"] == "Ada"
    assert full["last_name"] == "Lovelace"
    assert full["document_type"] == "Passport"

    payload = approved_with_identity()
    payload["decision"]["id_verifications"] = [
        {"first_name": "Grace", "date_of_birth": "1906-12-09"}
    ]

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] == "Grace"
    assert result["dob"] == "1906-12-09"
    assert result["last_name"] is None
    assert result["full_name"] is None
    assert result["document_type"] is None
    assert result["document_number"] is None
    assert result["document_country"] is None


def test_outcome_from_payload_issuing_state_maps_to_document_country_only():
    """Didit's issuing_state is the document country; it must not leak as its own key."""
    result = didit_client.outcome_from_payload(approved_with_identity())

    assert result["document_country"] == "GB"
    assert "issuing_state" not in result


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1990-05-04", "1990-05-04"),
        ("1990-05-04T12:30:00Z", "1990-05-04"),
        # Ambiguous separated dates are REFUSED, not guessed. "04/05/1990" is the 4th of
        # May to most of the world and the 5th of April in the US, and nothing in the
        # payload says which. A wrong guess is not caught anywhere downstream - it hashes
        # the wrong birthday and resurfaces later as a genuine customer failing
        # re-verification. None parks them for a human, which is the honest answer.
        ("04/05/1990", None),
        ("04-05-1990", None),
        ("not a date", None),
        ("", None),
        (None, None),
        (19900504, None),
    ],
)
def test_outcome_from_payload_normalises_dob(raw, expected):
    """Date-of-birth normalisation must match Stripe's YYYY-MM-DD format exactly."""
    payload = approved_with_identity(date_of_birth=raw)

    result = didit_client.outcome_from_payload(payload)

    assert result["dob"] == expected


def test_outcome_from_payload_full_name_only_fills_first_name_and_hash_matches_split_name():
    """A full_name-only record hashes like the split name through the sorted word set."""
    from app.services import kyc

    payload = approved_with_identity()
    payload["decision"]["id_verifications"] = [
        {"full_name": "Ada Lovelace", "date_of_birth": "1815-12-10"}
    ]

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] == "Ada Lovelace"
    assert result["last_name"] is None
    assert result["full_name"] == "Ada Lovelace"
    assert result["dob"] == "1815-12-10"

    full_name_hash = kyc.identity_hash(result["first_name"], result["last_name"], result["dob"])
    split_name_hash = kyc.identity_hash("Ada", "Lovelace", "1815-12-10")
    assert full_name_hash is not None
    assert full_name_hash == split_name_hash


def test_outcome_from_payload_blank_strings_are_treated_as_absent():
    """Whitespace-only name fields are None; the non-blank sibling still maps."""
    payload = approved_with_identity(first_name="   ")

    result = didit_client.outcome_from_payload(payload)

    assert result["first_name"] is None
    assert result["last_name"] == "Lovelace"
