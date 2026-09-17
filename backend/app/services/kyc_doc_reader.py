"""P43: the safety AI reads every uploaded verification document.

What the AI does: READ the document (type, names, address, date, company, number) and say
whether each value matches what the application declares. What it never does: decide.
The pass / warn / fail result is computed here in code from what was read, so a document
can't talk its way into "pass".

Per kind:
- proof_of_address   the owner's name + current residential address, dated within
                     KYC_ADDRESS_PROOF_MAX_DAYS, a utility bill / bank or card statement /
                     government or tax letter / lease.
- registration_certificate, articles   the company's legal name and registration number.
- tax_id_letter      the company's legal name and tax number.
- other              read and summarised only (never blocks).
"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import KycDocument, KycPerson, KycProfile
from app.services import ai_guard, kyc_documents

log = structlog.get_logger("kyc_doc_reader")

MAX_PDF_PAGES = 2
PDF_TEXT_ENOUGH = 200  # characters: a digital PDF; fewer = scanned, render the pages
ACCEPTED_ADDRESS_PROOF = (
    "utility_bill",
    "bank_statement",
    "card_statement",
    "government_letter",
    "tax_letter",
    "lease",
    "insurance_statement",
)

SYSTEM = """You read identity and business verification documents for a telecom company's
compliance team. You extract what is printed on the document and compare it with the
applicant's declared details. Be literal: only report what you can actually read.

Return JSON with exactly these keys:
{
  "readable": true/false,
  "document_type": one of "utility_bill","bank_statement","card_statement",
      "government_letter","tax_letter","lease","insurance_statement",
      "registration_certificate","articles","tax_id_letter","id_document","other",
  "issuer": string or null,
  "document_date": "YYYY-MM-DD" or null (statement/issue date printed on it),
  "names": [person or company names printed as the addressee/subject],
  "address": the addressee's full address as printed, or null,
  "company_name": registered company name, or null,
  "registration_number": company/registration/corporation number, or null,
  "tax_number": EIN / business number / VAT / UTR, or null,
  "jurisdiction": state/province/country of registration, or null,
  "company_status": status printed (e.g. "active", "good standing"), or null,
  "name_matches": true/false/null  (the declared PERSON name appears as addressee; allow
      middle names/initials and order differences; null if not applicable),
  "address_matches": true/false/null (same residential address as declared; allow
      abbreviations like St/Street, apartment formatting, postal code spacing),
  "company_matches": true/false/null (declared company legal name; allow LLC/Inc/Ltd
      punctuation differences),
  "number_matches": true/false/null (declared registration or tax number),
  "looks_edited": true/false (visible signs of tampering: mismatched fonts, pasted text,
      misaligned fields, cut-out areas),
  "notes": one or two short sentences for the compliance reviewer
}"""


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _address_text(address: dict | None) -> str:
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get("line1"),
        address.get("line2"),
        address.get("city"),
        address.get("region"),
        address.get("postal_code"),
        address.get("country"),
    ]
    return ", ".join(str(p) for p in parts if p)


def _pdf_parts(data: bytes) -> tuple[str, list[dict]]:
    """(text, image parts). Digital PDFs send text; scanned PDFs send rendered pages."""
    from pypdf import PdfReader

    text = ""
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    except Exception:  # noqa: BLE001 - fall through to rendering
        text = ""
    if len(text.strip()) >= PDF_TEXT_ENOUGH:
        return text[:12_000], []
    images: list[dict] = []
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        for index in range(min(len(pdf), MAX_PDF_PAGES)):
            bitmap = pdf[index].render(scale=1.6)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, "PNG")
            images.append(ai_guard.image_part(buffer.getvalue(), "image/png"))
    except Exception:  # noqa: BLE001 - unreadable PDF -> the AI says readable=false
        log.warning("kyc_pdf_render_failed")
    return text, images


def expected_for(document: KycDocument, profile: KycProfile, person: KycPerson | None) -> dict:
    expected: dict = {"document_kind": document.kind}
    if document.kind == "proof_of_address":
        expected["person_name"] = (person.verified_name or person.full_name) if person else None
        expected["residential_address"] = _address_text(
            person.residential_address if person else None
        )
    else:
        expected["company_legal_name"] = profile.legal_name
        expected["company_dba_name"] = profile.dba_name
        expected["country"] = profile.country
        expected["registered_address"] = _address_text(profile.registered_address)
        if document.kind == "tax_id_letter":
            expected["tax_number"] = profile.tax_id
        else:
            expected["registration_number"] = profile.registration_number
    return expected


def decide(
    settings: Settings, document: KycDocument, read: dict, *, today: date | None = None
) -> tuple[str, list[str]]:
    """Code, not the AI, turns what was read into pass / warn / fail with reasons."""
    today = today or _today()
    problems: list[str] = []
    unsure: list[str] = []
    if not read.get("readable", False):
        return "fail", ["The document could not be read. Upload a clearer copy."]
    if read.get("looks_edited"):
        problems.append("The document shows signs of editing.")

    if document.kind == "proof_of_address":
        if read.get("document_type") not in ACCEPTED_ADDRESS_PROOF:
            problems.append(
                "This isn't an accepted proof of address (utility bill, bank or card "
                "statement, government or tax letter, lease)."
            )
        doc_date = _parse_date(read.get("document_date"))
        max_days = settings.kyc_address_proof_max_days
        if doc_date is None:
            unsure.append("No date could be read on the document.")
        elif (today - doc_date).days > max_days:
            problems.append(f"The document is older than {max_days} days.")
        elif doc_date > today:
            problems.append("The document is dated in the future.")
        for key, label in (("name_matches", "name"), ("address_matches", "address")):
            if read.get(key) is False:
                problems.append(f"The {label} on the document doesn't match the one declared.")
            elif read.get(key) is None:
                unsure.append(f"The {label} could not be compared.")
    elif document.kind in ("registration_certificate", "articles", "tax_id_letter"):
        if read.get("company_matches") is False:
            problems.append("The company name doesn't match the legal name declared.")
        elif read.get("company_matches") is None:
            unsure.append("The company name could not be compared.")
        if read.get("number_matches") is False:
            problems.append("The number on the document doesn't match the one declared.")
        elif read.get("number_matches") is None:
            unsure.append("The registration or tax number could not be compared.")
        status = str(read.get("company_status") or "").lower()
        if any(word in status for word in ("dissolved", "inactive", "revoked", "struck")):
            problems.append(f"The document shows the company as {status}.")
    else:
        return "warn", ["Read for context only."]

    if problems:
        return "fail", problems
    if unsure:
        return "warn", unsure
    return "pass", []


async def review_document(
    session: AsyncSession,
    settings: Settings,
    object_store,
    profile: KycProfile,
    document: KycDocument,
    persons: list[KycPerson],
) -> KycDocument:
    person = next((p for p in persons if p.id == document.person_id), None)
    expected = expected_for(document, profile, person)
    data = await kyc_documents.read(settings, object_store, document)
    text, images = "", []
    if document.content_type == "application/pdf":
        text, images = _pdf_parts(data)
    else:
        images = [ai_guard.image_part(data, document.content_type)]
    user = "\n\n".join(
        part
        for part in (
            ai_guard.data_block("declared_details", expected),
            ai_guard.data_block("document_text", text) if text else "",
            "Read the document (text and/or images) and fill in the JSON.",
        )
        if part
    )
    now = datetime.now(timezone.utc)
    try:
        judgement = await ai_guard.judge(
            settings,
            task="kyc_document",
            system=SYSTEM,
            user=user,
            images=images or None,
            max_tokens=700,
        )
    except ai_guard.AIUnavailable as exc:
        document.review_result = "error"
        document.review = {"error": str(exc)[:200], "reasons": ["Automatic review is unavailable."]}
        document.reviewed_at = now
        return document
    result, reasons = decide(settings, document, judgement.data)
    document.review_result = result
    document.review = {
        "read": judgement.data,
        "expected": expected,
        "reasons": reasons,
        "tokens_in": judgement.tokens_in,
        "tokens_out": judgement.tokens_out,
    }
    document.reviewed_at = now
    return document


def customer_message(document: KycDocument) -> str | None:
    """What the applicant sees - the reasons, never the raw extraction."""
    if document.review_result in (None, "pass"):
        return None
    if document.review_result == "error":
        return "We'll review this document shortly."
    reasons = (document.review or {}).get("reasons") or []
    return " ".join(reasons)[:500] or None
