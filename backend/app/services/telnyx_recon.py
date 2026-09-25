from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

import httpx
import structlog
from sqlalchemy import delete, select

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Org, OrgNumber, TelnyxCostDaily
from app.providers.telnyx.numbers import is_csaas_owned

log = structlog.get_logger("telnyx_recon")


class TelnyxBillingError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Telnyx API error {status}: {body}")


async def api_key(session, settings) -> str | None:
    """Return the Telnyx API key from settings, or None when empty/missing."""
    secret = getattr(settings, "telnyx_api_key", None)
    if secret is None:
        return None
    try:
        value = secret.get_secret_value()
    except AttributeError:
        value = str(secret)
    return value if value else None


class TelnyxBilling:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.telnyx.com/v2",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        if self._owns_client:
            self._client = httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def list_billing_groups(self) -> list[dict]:
        groups: list[dict] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            resp = await self._client.get(
                f"{self.base_url}/billing_groups",
                params={"page[number]": page, "page[size]": 250},
                headers=self._headers(),
            )
            if not 200 <= resp.status_code < 300:
                raise TelnyxBillingError(resp.status_code, resp.text)
            payload = resp.json()
            data = payload.get("data") or []
            if isinstance(data, list):
                groups.extend(item for item in data if isinstance(item, dict))
            meta = payload.get("meta") or {}
            total_pages = int(meta.get("total_pages") or 1)
            page += 1
        return groups

    async def create_billing_group(self, name: str) -> str:
        resp = await self._client.post(
            f"{self.base_url}/billing_groups",
            json={"name": name},
            headers=self._headers(),
        )
        if not 200 <= resp.status_code < 300:
            raise TelnyxBillingError(resp.status_code, resp.text)
        data = (resp.json() or {}).get("data") or {}
        return str(data.get("id") or "")

    async def find_number(self, e164: str) -> dict | None:
        resp = await self._client.get(
            f"{self.base_url}/phone_numbers",
            params={"filter[phone_number]": e164},
            headers=self._headers(),
        )
        if not 200 <= resp.status_code < 300:
            raise TelnyxBillingError(resp.status_code, resp.text)
        data = (resp.json() or {}).get("data") or []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
            return None
        if isinstance(data, dict):
            return data
        return None

    async def set_number_billing_group(self, number_id: str, group_id: str) -> bool:
        resp = await self._client.patch(
            f"{self.base_url}/phone_numbers/{number_id}",
            json={"billing_group_id": group_id},
            headers=self._headers(),
        )
        if not 200 <= resp.status_code < 300:
            raise TelnyxBillingError(resp.status_code, resp.text)
        return True

    async def detail_records(self, record_type: str, day) -> list[dict]:
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        start_s = start.isoformat().replace("+00:00", "Z")
        end_s = end.isoformat().replace("+00:00", "Z")

        if record_type == "messaging":
            date_params = {
                "filter[created_at][gte]": start_s,
                "filter[created_at][lt]": end_s,
            }
        else:
            date_params = {
                "filter[started_at][gte]": start_s,
                "filter[started_at][lt]": end_s,
            }

        records: list[dict] = []
        page = 1
        total_pages = 1
        while page <= total_pages and page <= 200:
            params = {
                "filter[record_type]": record_type,
                "page[number]": page,
                "page[size]": 50,
                **date_params,
            }
            resp = await self._client.get(
                f"{self.base_url}/detail_records",
                params=params,
                headers=self._headers(),
            )
            if not 200 <= resp.status_code < 300:
                raise TelnyxBillingError(resp.status_code, resp.text)
            payload = resp.json()
            data = payload.get("data") or []
            if isinstance(data, list):
                records.extend(item for item in data if isinstance(item, dict))
            meta = payload.get("meta") or {}
            total_pages = int(meta.get("total_pages") or 1)
            page += 1
        return records

    async def balance(self) -> dict | None:
        resp = await self._client.get(
            f"{self.base_url}/balance",
            headers=self._headers(),
        )
        if not 200 <= resp.status_code < 300:
            raise TelnyxBillingError(resp.status_code, resp.text)
        data = (resp.json() or {}).get("data")
        return data if isinstance(data, dict) else None


def group_name(org) -> str:
    return f"csaas-org-{org.slug[:40]}-{str(org.id)[:8]}"


async def ensure_billing_groups(session, settings, *, client=None) -> dict:
    counts = {"groups_created": 0, "numbers_assigned": 0, "skipped_not_ours": 0, "errors": 0}
    key = await api_key(session, settings)
    if not key:
        return counts

    billing = TelnyxBilling(key, client=client)
    try:
        org_result = await session.execute(select(Org).order_by(Org.id))
        orgs = list(org_result.scalars().all())

        number_result = await session.execute(
            select(OrgNumber)
            .where(
                OrgNumber.carrier == "telnyx",
                OrgNumber.status == "active",
                OrgNumber.released_at.is_(None),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        numbers = list(number_result.scalars().all())

        by_org: dict = {}
        for number in numbers:
            by_org.setdefault(number.org_id, []).append(number)

        for org in orgs:
            active_numbers = by_org.get(org.id, [])
            if not active_numbers:
                continue

            try:
                gid = org.telnyx_billing_group_id
                if not gid:
                    target_name = group_name(org)
                    existing_groups = await billing.list_billing_groups()
                    for group in existing_groups:
                        if group.get("name") == target_name:
                            gid = str(group.get("id") or "")
                            break
                    if not gid:
                        gid = await billing.create_billing_group(target_name)
                        counts["groups_created"] += 1
                    org.telnyx_billing_group_id = gid
                    await session.commit()

                set_org_context(session, org.id)

                for number in active_numbers:
                    provisioning = number.provisioning or {}
                    if provisioning.get("telnyx_billing_group_id") == gid:
                        continue

                    row = await billing.find_number(number.e164)
                    if row is None or not is_csaas_owned(row):
                        counts["skipped_not_ours"] += 1
                        log.warning(
                            "telnyx_recon_skip_not_ours",
                            e164=number.e164,
                            org_id=str(org.id),
                        )
                        continue

                    row_billing_group_id = row.get("billing_group_id")
                    if str(row_billing_group_id) != str(gid):
                        number_id = str(row.get("id") or "")
                        if not number_id:
                            log.error(
                                "telnyx_recon_number_missing_id",
                                e164=number.e164,
                                org_id=str(org.id),
                            )
                            counts["errors"] += 1
                            continue
                        await billing.set_number_billing_group(number_id, gid)
                        counts["numbers_assigned"] += 1

                    number.provisioning = {
                        **(number.provisioning or {}),
                        "telnyx_billing_group_id": gid,
                    }

                await session.commit()
            except Exception:
                counts["errors"] += 1
                log.exception("telnyx_recon_org_failed", org_id=str(org.id))
                await session.rollback()

        return counts
    finally:
        await billing.aclose()


def _micros(value) -> int:
    """Convert a decimal-string dollar cost to integer micros. None/empty -> 0."""
    if value is None:
        return 0
    text = str(value).strip()
    if text == "":
        return 0
    micros = (Decimal(text) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(micros)


async def reconcile_day(session, settings, day, *, client=None) -> dict:
    key = await api_key(session, settings)
    if not key:
        return {"records": 0, "rows": 0, "unmatched_cost_micros": 0, "matched_cost_micros": 0}

    billing = TelnyxBilling(key, client=client)
    try:
        number_result = await session.execute(
            select(OrgNumber)
            .where(OrgNumber.carrier == "telnyx")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
        all_numbers = list(number_result.scalars().all())

        owner_by_e164: dict[str, object] = {}
        for number in all_numbers:
            if number.e164:
                owner_by_e164.setdefault(str(number.e164), number.org_id)

        typed_records: list[tuple[str, dict]] = []
        for record_type in ("messaging", "sip-trunking", "fax"):
            records = await billing.detail_records(record_type, day)
            typed_records.extend((record_type, record) for record in records)

        aggregates: dict[tuple, tuple[int, int]] = {}
        for record_type, record in typed_records:
            if not isinstance(record, dict):
                continue

            org_id = None
            for field in ("cli", "cld"):
                value = record.get(field)
                if value is not None:
                    matched = owner_by_e164.get(str(value))
                    if matched is not None:
                        org_id = matched
                        break

            if record_type == "messaging":
                message_type = str(record.get("message_type") or "").strip().upper()
                record_label = "mms" if message_type == "MMS" else "sms"
                parts = record.get("parts")
                try:
                    quantity = int(parts) if parts is not None else 1
                except (TypeError, ValueError):
                    quantity = 1
            elif record_type == "sip-trunking":
                record_label = "voice"
                billed_sec = record.get("billed_sec")
                try:
                    billed_sec_int = int(billed_sec or 0)
                except (TypeError, ValueError):
                    billed_sec_int = 0
                quantity = math.ceil(max(0, billed_sec_int) / 60)
            else:  # fax
                record_label = "fax"
                page_count = record.get("page_count")
                try:
                    quantity = int(page_count) if page_count is not None else 1
                except (TypeError, ValueError):
                    quantity = 1

            direction = str(record.get("direction") or "").strip().lower() or "unknown"
            cost_micros = _micros(record.get("cost"))

            key_agg = (org_id, record_label, direction)
            prior_qty, prior_cost = aggregates.get(key_agg, (0, 0))
            aggregates[key_agg] = (prior_qty + quantity, prior_cost + cost_micros)

        records_count = len(typed_records)
        rows = []
        unmatched_cost = 0
        matched_cost = 0
        for (org_id, record_label, direction), (quantity, cost_micros) in aggregates.items():
            rows.append(
                TelnyxCostDaily(
                    id=uuid4(),
                    org_id=org_id,
                    period_date=day,
                    record_type=record_label,
                    direction=direction,
                    quantity=quantity,
                    cost_micros=cost_micros,
                    created_at=datetime.utcnow(),
                )
            )
            if org_id is None:
                unmatched_cost += cost_micros
            else:
                matched_cost += cost_micros

        await session.execute(delete(TelnyxCostDaily).where(TelnyxCostDaily.period_date == day))
        session.add_all(rows)
        await session.commit()

        return {
            "records": records_count,
            "rows": len(rows),
            "unmatched_cost_micros": unmatched_cost,
            "matched_cost_micros": matched_cost,
        }
    finally:
        await billing.aclose()


async def nightly(settings) -> dict:
    """Run billing-group assignment and reconcile the two most recent UTC days.

    Never raises: logs and returns whatever counts were accumulated so far.
    """
    merged: dict[str, int] = {}
    try:
        from app.db.session import get_sessionmaker

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            try:
                key = await api_key(session, settings)
                if not key:
                    return {}

                ensure_counts = await ensure_billing_groups(session, settings)
                for k, v in ensure_counts.items():
                    merged[k] = merged.get(k, 0) + v

                yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
                for target_day in (yesterday, yesterday - timedelta(days=1)):
                    recon_counts = await reconcile_day(session, settings, target_day)
                    for k, v in recon_counts.items():
                        merged[k] = merged.get(k, 0) + v

                return merged
            except Exception:
                log.exception("telnyx_recon_nightly_failed")
                return merged
    except Exception:
        log.exception("telnyx_recon_nightly_session_failed")
        return {}
