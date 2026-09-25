"""Fax over Telnyx Programmable Fax (billing v2, B7).

Money (fax_page_out / fax_page_in, default $0.10/page):
- send: refuse unless the balance covers pages x price (402 + billing_refusals row), then HOLD
  that amount (reference faxres:<id>) and hand the document to Telnyx.
- fax.delivered: charge the carrier's page count once (reference fax:<id>), release the hold.
- fax.failed: release the hold, charge nothing.
- fax.received: download the document at once (Telnyx media URLs expire in minutes), store it,
  charge the pages (inbound is never refused - may take the balance below zero, like SMS).

Shared Telnyx account: a number is switched to fax mode only when its Telnyx tags carry
csaas (providers/telnyx/numbers.is_csaas_owned). Everything is idempotent on the carrier's
event id (fax_events) and fax id.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import FeatureUnavailableError, NotFoundError, ValidationFailedError
from app.models import Fax, FaxEvent, OrgNumber
from app.services import credits, telephony_billing

log = structlog.get_logger("fax")

BASE_URL = "https://api.telnyx.com/v2"
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 350
ALLOWED_TYPES = {"application/pdf": "pdf", "image/tiff": "tiff", "image/tif": "tiff"}
RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(settings) -> str:  # noqa: ANN001
    key = settings.telnyx_api_key.get_secret_value()
    if not key or not settings.telnyx_fax_connection_id:
        raise FeatureUnavailableError("Fax is not set up yet.")
    return key


def hold_reference(fax_id: uuid.UUID) -> str:
    return f"faxres:{fax_id}"


def count_pages(data: bytes, content_type: str) -> int:
    """Page count of a PDF or TIFF. Raises ValidationFailedError on anything unreadable."""
    try:
        if ALLOWED_TYPES.get(content_type) == "pdf":
            from pypdf import PdfReader

            pages = len(PdfReader(io.BytesIO(data)).pages)
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                pages = int(getattr(img, "n_frames", 1) or 1)
    except ValidationFailedError:
        raise
    except Exception as exc:
        raise ValidationFailedError("We could not read that document. Send a PDF or TIFF.") from exc
    if pages < 1:
        raise ValidationFailedError("That document has no pages.")
    if pages > MAX_PAGES:
        raise ValidationFailedError(f"A fax can have at most {MAX_PAGES} pages.")
    return pages


async def _price(session: AsyncSession, org_id: uuid.UUID, direction: str) -> int:
    metric = "fax_page_out" if direction == "outbound" else "fax_page_in"
    return await telephony_billing.unit_price(session, org_id, "telnyx", metric)


async def _org_fax_number(session: AsyncSession, org_id: uuid.UUID, e164: str) -> OrgNumber:
    set_org_context(session, org_id)
    number = (
        await session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.e164 == e164,
                OrgNumber.released_at.is_(None),
                OrgNumber.status == "active",
            )
        )
    ).scalar_one_or_none()
    if number is None:
        raise NotFoundError("That number is not one of yours.")
    if number.carrier != "telnyx" or not (number.provisioning or {}).get("fax_mode"):
        raise ValidationFailedError("Turn on fax for this number first (Numbers > Fax line).")
    return number


async def send(
    session: AsyncSession,
    settings,  # noqa: ANN001
    store,  # noqa: ANN001
    org_id: uuid.UUID,
    *,
    from_e164: str,
    to_e164: str,
    data: bytes,
    content_type: str,
    filename: str,
    user_id: uuid.UUID | None,
    client: httpx.AsyncClient | None = None,
) -> Fax:
    """Validate, hold the money, store the document, hand it to Telnyx. Commits."""
    key = _key(settings)
    if content_type not in ALLOWED_TYPES:
        raise ValidationFailedError("Send a PDF or TIFF.")
    if len(data) > MAX_BYTES:
        raise ValidationFailedError("A fax document can be at most 20 MB.")
    await _org_fax_number(session, org_id, from_e164)
    pages = count_pages(data, content_type)
    price = await _price(session, org_id, "outbound") * pages

    fax = Fax(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        status="queued",
        from_e164=from_e164,
        to_e164=to_e164,
        page_count=pages,
        media_name=(filename or "fax")[:255],
        created_by=user_id,
    )
    if await telephony_billing.is_prepaid(session, org_id):
        balance = await credits.balance(session, org_id)
        if balance < max(price, 1):
            await telephony_billing.record_refusal(
                session, org_id, kind="fax", price_micros=price, balance_micros=balance,
                detail=to_e164,
            )
            raise telephony_billing.TelephonyCreditsError()
        await credits.reserve(session, org_id, price, reference=hold_reference(fax.id))

    fax.media_key = f"fax/{org_id}/{fax.id}.{ALLOWED_TYPES[content_type]}"
    await store.put(fax.media_key, data, content_type)
    set_org_context(session, org_id)
    session.add(fax)
    await session.commit()

    own = client is None
    http = client or httpx.AsyncClient(timeout=60)
    try:
        resp = await http.post(
            f"{BASE_URL}/faxes",
            headers={"Authorization": f"Bearer {key}"},
            data={
                "connection_id": settings.telnyx_fax_connection_id,
                "from": from_e164,
                "to": to_e164,
                "client_state": str(fax.id),
            },
            files={"contents": (fax.media_name, data, content_type)},
        )
        body = resp.json() if resp.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        resp, body = None, {"error": str(exc)}
    finally:
        if own:
            await http.aclose()

    set_org_context(session, org_id)
    fax = await session.get(Fax, fax.id)
    # The webhook may have raced us (matched via client_state) - read what it committed.
    await session.refresh(fax)
    if fax.status != "queued":
        return fax
    if resp is None or resp.status_code >= 300:
        fax.status = "failed"
        fax.failure_reason = _error_text(body)[:255] or "The fax service refused it."
        fax.completed_at = _now()
        await credits.release(session, org_id, reference=hold_reference(fax.id))
        await session.commit()
        return fax
    data_obj = (body or {}).get("data") or {}
    fax.provider_fax_id = str(data_obj.get("id") or "") or None
    fax.status = "sending"
    await session.commit()
    return fax


def _error_text(body: object) -> str:
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            return str(errs[0].get("detail") or errs[0].get("title") or "")
        return str(body.get("error") or "")
    return ""


async def _record_event(session: AsyncSession, data: dict) -> bool:
    """True when this event id is new (first delivery)."""
    event_id = str(data.get("id") or "")[:64]
    if not event_id:
        return True
    if await session.get(FaxEvent, event_id) is not None:
        return False
    payload = data.get("payload") or {}
    try:
        async with session.begin_nested():
            session.add(
                FaxEvent(
                    id=event_id,
                    event_type=str(data.get("event_type") or "")[:48],
                    provider_fax_id=str(payload.get("fax_id") or "")[:64] or None,
                    payload=payload,
                    received_at=_now(),
                )
            )
            await session.flush()
    except IntegrityError:
        return False
    return True


async def _fax_by_provider_id(session: AsyncSession, provider_fax_id: str) -> Fax | None:
    return (
        await session.execute(
            sa.select(Fax)
            .where(Fax.provider_fax_id == provider_fax_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()


async def handle_webhook(
    session: AsyncSession,
    settings,  # noqa: ANN001
    store,  # noqa: ANN001
    body: dict,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Apply one verified Telnyx fax webhook. Returns a short outcome label. Commits."""
    data = (body or {}).get("data") or {}
    event_type = str(data.get("event_type") or "")
    payload = data.get("payload") or {}
    fax_id = str(payload.get("fax_id") or "")
    if not event_type.startswith("fax.") or not fax_id:
        return "ignored"
    if not await _record_event(session, data):
        await session.rollback()
        return "duplicate"

    direction = str(payload.get("direction") or "")
    fax = await _fax_by_provider_id(session, fax_id)
    if fax is None and direction == "outbound":
        # Webhook raced the send's commit: match on our own id in client_state.
        state = str(payload.get("client_state") or "")
        try:
            candidate = uuid.UUID(state)
        except ValueError:
            candidate = None
        if candidate is not None:
            fax = (
                await session.execute(
                    sa.select(Fax)
                    .where(Fax.id == candidate)
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            ).scalar_one_or_none()
            if fax is not None and not fax.provider_fax_id:
                fax.provider_fax_id = fax_id

    if direction == "inbound" or (fax is None and event_type.startswith("fax.receiv")):
        outcome = await _inbound(session, settings, store, fax, payload, event_type, client=client)
        await session.commit()
        return outcome
    if fax is None:
        await session.commit()
        return "unknown_fax"

    set_org_context(session, fax.org_id)
    if event_type == "fax.delivered" and fax.status != "delivered":
        pages = int(payload.get("page_count") or fax.page_count or 1)
        fax.page_count = pages
        fax.status = "delivered"
        fax.completed_at = _now()
        await credits.release(session, fax.org_id, reference=hold_reference(fax.id))
        if await telephony_billing.is_prepaid(session, fax.org_id):
            price = await _price(session, fax.org_id, "outbound") * pages
            if price > 0:
                await credits.charge_usage(
                    session, fax.org_id, price, reference=f"fax:{fax.id}",
                    note=f"Fax sent, {pages} page(s)",
                )
                fax.charged_micros = price
    elif event_type == "fax.failed" and fax.status not in ("delivered", "failed"):
        fax.status = "failed"
        fax.failure_reason = str(payload.get("failure_reason") or "failed")[:255]
        fax.completed_at = _now()
        await credits.release(session, fax.org_id, reference=hold_reference(fax.id))
    elif event_type in ("fax.sending.started", "fax.media.processed", "fax.queued"):
        if fax.status == "queued":
            fax.status = "sending"
    await session.commit()
    return event_type


async def _inbound(
    session: AsyncSession, settings, store, fax: Fax | None, payload: dict, event_type: str,  # noqa: ANN001
    *, client: httpx.AsyncClient | None,
) -> str:
    to = str(payload.get("to") or "")
    from_ = str(payload.get("from") or "")
    if fax is None:
        number = (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.e164 == to, OrgNumber.released_at.is_(None))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if number is None:
            log.warning("fax_inbound_unknown_number", to=to)
            return "unknown_number"
        set_org_context(session, number.org_id)
        fax = Fax(
            id=uuid.uuid4(),
            org_id=number.org_id,
            direction="inbound",
            status="receiving",
            from_e164=from_[:20],
            to_e164=to[:20],
            provider_fax_id=str(payload.get("fax_id")),
        )
        session.add(fax)
        await session.flush()
    set_org_context(session, fax.org_id)
    if event_type == "fax.failed":
        fax.status = "failed"
        fax.failure_reason = str(payload.get("failure_reason") or "failed")[:255]
        fax.completed_at = _now()
        return "inbound_failed"
    if event_type != "fax.received" or fax.status == "received":
        return "inbound_progress"

    pages = int(payload.get("page_count") or 1)
    fax.page_count = pages
    media_url = str(payload.get("media_url") or "")
    if media_url:
        own = client is None
        http = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
        try:
            resp = await http.get(media_url)
            if resp.status_code == 200 and resp.content:
                ctype = resp.headers.get("content-type", "application/pdf").split(";")[0]
                ext = ALLOWED_TYPES.get(ctype, "pdf")
                fax.media_key = f"fax/{fax.org_id}/{fax.id}.{ext}"
                await store.put(fax.media_key, resp.content, ctype)
                fax.media_name = f"fax-from-{from_ or 'unknown'}.{ext}"
            else:
                log.warning("fax_inbound_download_failed", status=resp.status_code)
        except httpx.HTTPError:
            log.exception("fax_inbound_download_error")
        finally:
            if own:
                await http.aclose()
    fax.status = "received"
    fax.completed_at = _now()
    if await telephony_billing.is_prepaid(session, fax.org_id):
        price = await _price(session, fax.org_id, "inbound") * pages
        if price > 0:
            await credits.charge_usage(
                session, fax.org_id, price, reference=f"fax:{fax.id}",
                note=f"Fax received, {pages} page(s)",
            )
            fax.charged_micros = price
    return "received"


async def set_fax_mode(
    session: AsyncSession, settings, number: OrgNumber, enabled: bool,  # noqa: ANN001
    *, client: httpx.AsyncClient | None = None,
) -> OrgNumber:
    """Move a csaas-owned Telnyx number onto the Fax Application (or back onto the voice
    connection). A number has ONE connection: in fax mode it does not take voice calls."""
    key = _key(settings)
    if number.carrier != "telnyx":
        raise ValidationFailedError("Fax is available on Telnyx numbers only.")
    from app.providers.telnyx.numbers import is_csaas_owned

    own = client is None
    http = client or httpx.AsyncClient(timeout=30)
    headers = {"Authorization": f"Bearer {key}"}
    try:
        resp = await http.get(
            f"{BASE_URL}/phone_numbers", params={"filter[phone_number]": number.e164},
            headers=headers,
        )
        rows = (resp.json() or {}).get("data") or [] if resp.status_code == 200 else []
        row = next(
            (r for r in rows if isinstance(r, dict) and r.get("phone_number") == number.e164),
            None,
        )
        if row is None or not is_csaas_owned(row):
            raise ValidationFailedError("That number cannot be switched here.")
        prov = dict(number.provisioning or {})
        if enabled:
            target = settings.telnyx_fax_connection_id
            if row.get("connection_id") and row.get("connection_id") != target:
                prov["voice_connection_before_fax"] = row.get("connection_id")
        else:
            target = prov.get("voice_connection_before_fax") or settings.telnyx_voice_connection_id
        resp = await http.patch(
            f"{BASE_URL}/phone_numbers/{row['id']}", json={"connection_id": target},
            headers=headers,
        )
        if resp.status_code >= 300:
            raise ValidationFailedError("The carrier refused the change. Try again shortly.")
    finally:
        if own:
            await http.aclose()
    prov["fax_mode"] = bool(enabled)
    number.provisioning = prov
    return number


async def purge_old_media(session: AsyncSession, store) -> int:  # noqa: ANN001
    """Delete stored fax documents older than RETENTION (rows stay, media_key cleared)."""
    rows = (
        await session.execute(
            sa.select(Fax)
            .where(Fax.media_key.is_not(None), Fax.created_at < _now() - RETENTION)
            .limit(500)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    done = 0
    for fax in rows:
        try:
            await store.delete(fax.media_key)
        except Exception:
            log.warning("fax_media_delete_failed", key=fax.media_key)
        set_org_context(session, fax.org_id)
        fax.media_key = None
        done += 1
    await session.commit()
    return done
