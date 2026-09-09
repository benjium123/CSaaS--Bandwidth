"""Pure parsing/validation core for contact-list import (P11 DR-8/DR-9). \
No DB, no IO beyond the passed bytes."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import openpyxl
import phonenumbers

from app.errors import ValidationFailedError


def _check_no_duplicate_headers(headers: list[str]) -> None:
    """6.23: a duplicate header silently drops one of the two columns when a row dict is
    built from ``zip(headers, row)`` - the LATER value wins with no signal that the
    earlier one (and everything mapped to it) was thrown away. Refuse it outright."""
    seen: set[str] = set()
    for header in headers:
        if header and header in seen:
            raise ValidationFailedError(f"Duplicate column header: {header!r}")
        seen.add(header)


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[dict[str, str]]

    @property
    def preview(self) -> list[dict[str, str]]:
        """First five parsed rows."""
        return self.rows[:5]


def _xlsx_value_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def parse_csv_bytes(data: bytes, max_rows: int | None = None) -> ParsedFile:
    """Parse CSV bytes into a ParsedFile.

    6.7: when ``max_rows`` is given, parsing ABORTS (raises) the moment it is exceeded,
    rather than building the full in-memory row list first and only checking its length
    afterward - a hostile/oversized file must not be fully materialized just to reject it.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    headers: list[str] = []
    rows: list[dict[str, str]] = []

    for raw_row in reader:
        row = [cell.strip() for cell in raw_row]
        if not headers:
            if not any(row):
                continue
            headers = row
            _check_no_duplicate_headers(headers)
            continue
        if not any(row):
            continue
        if len(row) < len(headers):
            row.extend([""] * (len(headers) - len(row)))
        elif len(row) > len(headers):
            row = row[: len(headers)]
        rows.append(dict(zip(headers, row, strict=True)))
        if max_rows is not None and len(rows) > max_rows:
            raise ValidationFailedError(f"List has too many rows; the limit is {max_rows}")

    return ParsedFile(headers=headers, rows=rows)


def parse_xlsx_bytes(data: bytes, max_rows: int | None = None) -> ParsedFile:
    """Parse the first XLSX worksheet into a ParsedFile. See ``parse_csv_bytes`` for the
    ``max_rows`` early-abort contract (6.7)."""
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        worksheet = workbook.worksheets[0]
        headers: list[str] = []
        rows: list[dict[str, str]] = []

        for raw_row in worksheet.iter_rows(values_only=True):
            if not headers:
                if all(cell is None for cell in raw_row):
                    continue
                headers = [str(cell).strip() if cell is not None else "" for cell in raw_row]
                _check_no_duplicate_headers(headers)
                continue

            row = [_xlsx_value_to_str(cell) for cell in raw_row]
            if not any(row):
                continue
            if len(row) < len(headers):
                row.extend([""] * (len(headers) - len(row)))
            elif len(row) > len(headers):
                row = row[: len(headers)]
            rows.append(dict(zip(headers, row, strict=True)))
            if max_rows is not None and len(rows) > max_rows:
                raise ValidationFailedError(f"List has too many rows; the limit is {max_rows}")

        return ParsedFile(headers=headers, rows=rows)
    finally:
        workbook.close()


FIELD_SYNONYMS: dict[str, list[str]] = {
    "phone": [
        "phone",
        "phone number",
        "mobile",
        "cell",
        "cell phone",
        "mobile phone",
        "number",
        "primary phone",
        "phone1",
        "telephone",
    ],
    "first_name": ["first name", "first", "fname", "given name"],
    "last_name": ["last name", "last", "lname", "surname", "family name"],
    "email": ["email", "e-mail", "email address"],
    "company": ["company", "business", "organization", "organisation"],
    # P11 auto-texter: a per-row message column - the uploaded sheet can carry the text
    # to send to that specific contact (user directive 2026-08-29).
    "message": ["message", "text", "sms", "body", "text message", "custom message", "sms text"],
    # P22: optional per-row owner (email), resolved to a member id in list_import.py.
    "owner": ["owner", "owner email", "assigned to", "owner_email"],
}


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    """Suggest canonical field names from header synonyms."""
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for field, synonyms in FIELD_SYNONYMS.items():
        for header in headers:
            normalized = header.lower().strip()
            if normalized in synonyms and header not in used:
                mapping[field] = header
                used.add(header)
                break

    return mapping


def normalize_phone(raw: str, region: str = "US") -> tuple[str | None, str | None]:
    """Normalize a phone number to E.164 or return an error reason."""
    if raw is None or not raw.strip():
        return (None, "missing phone")

    try:
        number = phonenumbers.parse(raw.strip(), region)
    except phonenumbers.NumberParseException:
        return (None, "unparseable phone")

    if not phonenumbers.is_valid_number(number):
        return (None, "invalid phone")

    return (
        phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164),
        None,
    )


def extract_row(raw_row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    """Extract mapped fields from a raw row, stripping values."""
    return {field: raw_row.get(header, "").strip() for field, header in mapping.items()}
