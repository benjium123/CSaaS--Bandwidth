"""The reviewer's view of a Didit decision: the answers that matter, nothing else."""

from __future__ import annotations

import json

from app.services.didit_evidence import summarize

RAW = {
    "session_id": "s1",
    "session_number": 1042,
    "status": "In Review",
    "created_at": "2026-09-20T10:00:00Z",
    "workflow_id": "wf",
    "features": ["ID_VERIFICATION", "LIVENESS", "FACE_MATCH", "AML", "IP_ANALYSIS"],
    "id_verifications": [
        {
            "status": "Approved",
            "document_type": "Passport",
            "document_number": "X12345678",
            "first_name": "Ada",
            "last_name": "Solo",
            "full_name": "Ada Solo",
            "date_of_birth": "1990-04-02",
            "age": 36,
            "expiration_date": "2020-01-01",
            "issuing_state": "USA",
            "issuing_state_name": "United States",
            "nationality": "USA",
            "front_image": "https://service-didit-x.s3.amazonaws.com/front.jpg",
            "back_image": "https://service-didit-x.s3.amazonaws.com/back.jpg",
            "portrait_image": "https://service-didit-x.s3.amazonaws.com/portrait.jpg",
            "mrz": {"line1": "P<USASOLO<<ADA"},
            "extra_fields": {"blood_type": "O"},
            "warnings": [
                {"risk": "high", "short_description": "Document expired"},
                {"risk": "high", "short_description": "Document expired"},
            ],
        }
    ],
    "liveness_checks": [
        {
            "status": "Approved",
            "score": 97.456,
            "reference_image": "https://service-didit-x.s3.amazonaws.com/selfie.jpg",
            "face_quality": 88,
        }
    ],
    "face_matches": [{"status": "Approved", "score": 91.2}],
    "aml_screenings": [
        {
            "status": "In Review",
            "total_hits": 1,
            "hits": [{"caption": "Ada Solo", "topics": ["sanction"], "match_score": 0.82}],
            "next_ongoing_monitoring_bill_date": "2027-01-01",
        }
    ],
    "ip_analyses": [
        {
            "status": "Approved",
            "ip_city": "Austin",
            "ip_country": "United States",
            "platform": "mobile",
            "os_family": "iOS",
            "device_fingerprint": "abc",
            "warnings": [{"risk": "low", "short_description": "VPN not detected"}],
        }
    ],
}


def test_distils_the_decision_to_what_a_reviewer_needs():
    s = summarize(RAW)
    assert s["verdict"] == "review"
    assert s["identity"] == {
        "full_name": "Ada Solo",
        "date_of_birth": "1990-04-02",
        "age": 36,
        "nationality": "USA",
        "document_type": "Passport",
        "document_number": "•••• 5678",
        "issuing_country": "United States",
        "expiration_date": "2020-01-01",
        "expired": True,
    }
    by_key = {c["key"]: c for c in s["checks"]}
    assert by_key["liveness"]["score"] == 97.5
    assert by_key["face_match"]["verdict"] == "ok"
    assert by_key["aml"]["verdict"] == "fail" and by_key["aml"]["detail"] == "1 possible match"
    assert by_key["location"]["detail"] == "Austin, United States"
    assert s["aml_matches"] == [{"name": "Ada Solo", "lists": ["sanction"], "score": 0.8}]
    assert [w["message"] for w in s["warnings"]] == ["Document expired", "VPN not detected"]
    assert [p["label"] for p in s["photos"]] == ["Selfie", "ID front", "ID back"]
    assert s["photos"][0]["path"] == "liveness_checks.0.reference_image"


def test_nothing_else_leaves_the_server():
    text = json.dumps(summarize(RAW))
    for leaked in (
        "P<USASOLO",
        "blood_type",
        "device_fingerprint",
        "abc",
        "X12345678",
        "amazonaws",
        "monitoring_bill",
        "face_quality",
    ):
        assert leaked not in text, leaked


def test_an_unfinished_session_is_empty_not_an_error():
    s = summarize({"status": "Not Started"})
    assert s["checks"] == [] and s["photos"] == [] and s["identity"]["full_name"] is None
