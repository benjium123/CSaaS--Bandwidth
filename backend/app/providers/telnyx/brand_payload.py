"""Pure mapping from a local Brand to a Telnyx 10DLC brand payload."""

from __future__ import annotations

from typing import Any

from app.errors import ValidationFailedError

#: Telnyx/TCR brandRelationship values. Anything else is rejected downstream.
BRAND_RELATIONSHIPS = frozenset(
    {
        "BASIC_ACCOUNT",
        "SMALL_ACCOUNT",
        "MEDIUM_ACCOUNT",
        "LARGE_ACCOUNT",
        "KEY_ACCOUNT",
    }
)

#: Telnyx/TCR entityType values. A sole proprietor IS the legal entity: EIN is optional.
ENTITY_TYPES = frozenset(
    {
        "PRIVATE_PROFIT",
        "PUBLIC_PROFIT",
        "NON_PROFIT",
        "GOVERNMENT",
        "SOLE_PROPRIETOR",
    }
)


def _clean(value: Any, field: str) -> str | None:
    """Trim a string field; non-strings are refused, never coerced with str()."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationFailedError(f"Brand field {field} must be a string")
    text = value.strip()
    return text or None


def _require(value: Any, field: str) -> str:
    text = _clean(value, field)
    if text is None:
        # Field names only: never echo the value (it may be PII).
        raise ValidationFailedError(f"Missing required brand field: {field}")
    return text


def _maybe(payload: dict[str, Any], field: str, value: Any) -> None:
    text = _clean(value, field)
    if text is not None:
        payload[field] = text


def build_brand_payload(
    brand: Any,
    *,
    company_name: Any,
    first_name: Any,
    last_name: Any,
    brand_relationship: Any,
) -> dict:
    """Map ``brand`` plus caller-supplied legal/contact names to Telnyx camelCase."""
    relationship = _require(brand_relationship, "brandRelationship").upper()
    if relationship not in BRAND_RELATIONSHIPS:
        raise ValidationFailedError("brandRelationship is not a supported Telnyx value")

    entity_type = _require(brand.entity_type, "entityType").upper()
    if entity_type not in ENTITY_TYPES:
        raise ValidationFailedError("entityType is not a supported Telnyx value")

    payload: dict[str, Any] = {
        "entityType": entity_type,
        "brandRelationship": relationship,
        "displayName": _require(brand.name, "displayName"),
        "vertical": _require(brand.vertical, "vertical").upper(),
        "email": _require(brand.email, "email"),
        "street": _require(brand.street, "street"),
        "city": _require(brand.city, "city"),
        "state": _require(brand.state, "state"),
        "postalCode": _require(brand.postal_code, "postalCode"),
        "country": _require(brand.country, "country").upper(),
    }

    if entity_type == "SOLE_PROPRIETOR":
        payload["firstName"] = _require(first_name, "firstName")
        payload["lastName"] = _require(last_name, "lastName")
        _maybe(payload, "companyName", company_name)
        _maybe(payload, "ein", brand.ein)
    else:
        payload["companyName"] = _require(company_name, "companyName")
        payload["ein"] = _require(brand.ein, "ein")
        _maybe(payload, "firstName", first_name)
        _maybe(payload, "lastName", last_name)

    _maybe(payload, "phone", brand.phone)
    _maybe(payload, "website", brand.website)

    return payload
