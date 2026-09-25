"""The part of a Didit decision a reviewer needs to approve or refuse someone.

Didit's decision is a document of dozens of sections (MRZ, device fingerprints, image
quality metrics, ongoing-monitoring billing dates...). A reviewer needs five answers: who
is this, is the document genuine and in date, is the person live and the same face, are
they on a sanctions or PEP list, and where did they verify from. This module distils the
decision to exactly that, plus the three photos. Nothing else leaves the server.

Photos are referenced by their PATH inside the raw decision (``id_verifications.0.
front_image``), which the existing evidence-media route resolves and streams - so the
short-lived signed URLs never reach the browser either.
"""

from __future__ import annotations

from datetime import date
from typing import Any

#: Didit statuses read as a verdict: ok / review / fail / none.
_OK = {"approved", "clear", "verified", "passed"}
_FAIL = {"declined", "rejected", "failed", "match"}


def _first(raw: dict, key: str) -> dict:
    items = raw.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _score(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 1)
    return None


def _verdict(status: Any) -> str:
    text = (_text(status) or "").lower()
    if not text:
        return "none"
    if text in _OK:
        return "ok"
    if text in _FAIL:
        return "fail"
    return "review"


def _masked(number: Any) -> str | None:
    text = _text(number)
    if not text:
        return None
    return f"•••• {text[-4:]}" if len(text) > 4 else text


def _expired(expiration: Any) -> bool | None:
    text = _text(expiration)
    if not text or len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10]) < date.today()
    except ValueError:
        return None


def _photo(raw: dict, section: str, field: str, label: str) -> dict | None:
    value = _first(raw, section).get(field)
    if isinstance(value, str) and value.startswith("https://"):
        return {"label": label, "path": f"{section}.0.{field}"}
    return None


def summarize(raw: dict) -> dict:
    idv = _first(raw, "id_verifications")
    live = _first(raw, "liveness_checks")
    face = _first(raw, "face_matches")
    aml = _first(raw, "aml_screenings")
    ip = _first(raw, "ip_analyses")

    checks = []
    if idv:
        checks.append(
            {
                "key": "document",
                "label": "ID document",
                "status": _text(idv.get("status")),
                "verdict": _verdict(idv.get("status")),
                "score": None,
                "detail": None,
            }
        )
    if live:
        checks.append(
            {
                "key": "liveness",
                "label": "Live person",
                "status": _text(live.get("status")),
                "verdict": _verdict(live.get("status")),
                "score": _score(live.get("score")),
                "detail": None,
            }
        )
    if face:
        checks.append(
            {
                "key": "face_match",
                "label": "Selfie matches ID",
                "status": _text(face.get("status")),
                "verdict": _verdict(face.get("status")),
                "score": _score(face.get("score")),
                "detail": None,
            }
        )
    if aml:
        hits = aml.get("total_hits")
        hits = hits if isinstance(hits, int) else None
        checks.append(
            {
                "key": "aml",
                "label": "Sanctions & PEP",
                "status": _text(aml.get("status")),
                "verdict": "fail" if hits else _verdict(aml.get("status")),
                "score": None,
                "detail": None
                if hits is None
                else f"{hits} possible match{'es' if hits != 1 else ''}",
            }
        )
    if ip:
        where = ", ".join(v for v in (_text(ip.get("ip_city")), _text(ip.get("ip_country"))) if v)
        checks.append(
            {
                "key": "location",
                "label": "Location & device",
                "status": _text(ip.get("status")),
                "verdict": _verdict(ip.get("status")),
                "score": None,
                "detail": where or None,
            }
        )

    warnings: list[dict] = []
    seen: set[str] = set()
    for section in (
        "id_verifications",
        "liveness_checks",
        "face_matches",
        "aml_screenings",
        "ip_analyses",
        "phone_verifications",
        "email_verifications",
    ):
        for warning in _first(raw, section).get("warnings") or []:
            if not isinstance(warning, dict):
                continue
            message = _text(warning.get("short_description")) or _text(warning.get("log_type"))
            if not message or message in seen:
                continue
            seen.add(message)
            warnings.append({"message": message, "risk": _text(warning.get("risk")) or "info"})
    warnings.sort(key=lambda w: {"high": 0, "medium": 1, "low": 2}.get(w["risk"].lower(), 3))

    aml_matches = []
    for hit in (aml.get("hits") or [])[:3]:
        if isinstance(hit, dict):
            name = _text(hit.get("caption")) or _text(hit.get("name"))
            if name:
                topics = hit.get("topics") or hit.get("datasets") or []
                aml_matches.append(
                    {
                        "name": name,
                        "lists": [t for t in topics if isinstance(t, str)][:3],
                        "score": _score(hit.get("match_score") or hit.get("score")),
                    }
                )

    photos = [
        p
        for p in (
            _photo(raw, "liveness_checks", "reference_image", "Selfie")
            or _photo(raw, "id_verifications", "portrait_image", "Photo on ID"),
            _photo(raw, "id_verifications", "front_image", "ID front"),
            _photo(raw, "id_verifications", "back_image", "ID back"),
        )
        if p
    ]

    return {
        "status": _text(raw.get("status")),
        "verdict": _verdict(raw.get("status")),
        "session_number": raw.get("session_number"),
        "created_at": _text(raw.get("created_at")),
        "identity": {
            "full_name": _text(idv.get("full_name"))
            or " ".join(v for v in (_text(idv.get("first_name")), _text(idv.get("last_name"))) if v)
            or None,
            "date_of_birth": _text(idv.get("date_of_birth")),
            "age": idv.get("age") if isinstance(idv.get("age"), int) else None,
            "nationality": _text(idv.get("nationality")),
            "document_type": _text(idv.get("document_type")),
            "document_number": _masked(idv.get("document_number")),
            "issuing_country": _text(idv.get("issuing_state_name"))
            or _text(idv.get("issuing_state")),
            "expiration_date": _text(idv.get("expiration_date")),
            "expired": _expired(idv.get("expiration_date")),
        },
        "checks": checks,
        "warnings": warnings[:8],
        "aml_matches": aml_matches,
        "photos": photos,
        "device": " · ".join(
            v
            for v in (
                _text(ip.get("platform")),
                _text(ip.get("os_family")),
                _text(ip.get("browser_family")),
            )
            if v
        )
        or None,
    }
