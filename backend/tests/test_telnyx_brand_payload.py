"""Unit tests for :func:`app.providers.telnyx.brand_payload.build_brand_payload`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.errors import ValidationFailedError
from app.providers.telnyx.brand_payload import build_brand_payload


def _brand(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "entity_type": "PRIVATE_PROFIT",
        "name": "Acme Widgets",
        "vertical": "Technology",
        "email": "ops@acme.example",
        "street": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "postal_code": "78701",
        "country": "us",
        "ein": "123456789",
        "phone": "+15125550100",
        "website": "https://acme.example",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(brand: Any, **kwargs: Any) -> dict:
    defaults = {"company_name": None, "first_name": None, "last_name": None}
    defaults["brand_relationship"] = "SMALL_ACCOUNT"
    return build_brand_payload(brand, **{**defaults, **kwargs})


def test_corporate_maps_to_telnyx_camel_case() -> None:
    assert _build(_brand(), company_name="Acme Widgets Inc") == {
        "entityType": "PRIVATE_PROFIT",
        "brandRelationship": "SMALL_ACCOUNT",
        "displayName": "Acme Widgets",
        "vertical": "TECHNOLOGY",
        "email": "ops@acme.example",
        "street": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "postalCode": "78701",
        "country": "US",
        "companyName": "Acme Widgets Inc",
        "ein": "123456789",
        "phone": "+15125550100",
        "website": "https://acme.example",
    }


def test_sole_proprietor_keeps_contact_and_optional_company_ein() -> None:
    brand = _brand(entity_type="SOLE_PROPRIETOR", ein=None)
    payload = _build(brand, first_name="Dana", last_name="Sole", mobile_phone="+15125550199")
    assert payload["entityType"] == "SOLE_PROPRIETOR"
    assert (payload["firstName"], payload["lastName"]) == ("Dana", "Sole")
    # TCR texts the verification PIN here.
    assert payload["mobilePhone"] == "+15125550199"
    assert "companyName" not in payload and "ein" not in payload


def test_missing_required_fields_rejected_by_name() -> None:
    cases = (
        ({"entity_type": None}, "entityType", "Acme Widgets Inc"),
        ({"name": None}, "displayName", "Acme Widgets Inc"),
        ({"postal_code": None}, "postalCode", "Acme Widgets Inc"),
        ({"ein": None}, "ein", "Acme Widgets Inc"),
        ({}, "companyName", None),
    )
    for overrides, field, company_name in cases:
        with pytest.raises(ValidationFailedError) as exc:
            _build(_brand(**overrides), company_name=company_name)
        assert field in str(exc.value)


def test_sole_proprietor_needs_a_mobile_for_the_verification_pin() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        _build(_brand(entity_type="SOLE_PROPRIETOR", ein=None), first_name="Dana", last_name="Sole")
    assert "mobilePhone" in str(exc.value)


def test_explicit_contact_names_required_for_sole_proprietor() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        _build(_brand(entity_type="SOLE_PROPRIETOR"), first_name="   ")
    assert "firstName" in str(exc.value)


def test_invalid_enums_rejected_without_echoing_values() -> None:
    for brand, kwargs in (
        (_brand(), {"brand_relationship": "top-secret-partner"}),
        (_brand(entity_type="alien-corp-42"), {}),
    ):
        with pytest.raises(ValidationFailedError) as exc:
            _build(brand, company_name="Acme Widgets Inc", **kwargs)
        assert "secret" not in str(exc.value).lower()
        assert "alien" not in str(exc.value).lower()


def test_non_string_values_rejected_not_coerced() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        _build(_brand(name=12345), company_name="Acme Widgets Inc")
    assert "displayName" in str(exc.value) and "12345" not in str(exc.value)
    with pytest.raises(ValidationFailedError) as exc:
        _build(_brand(), company_name=987654321)
    assert "companyName" in str(exc.value) and "987654321" not in str(exc.value)
