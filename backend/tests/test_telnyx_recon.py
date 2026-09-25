from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.base import set_org_context
from app.models import Org, OrgNumber, TelnyxCostDaily
from app.services.telnyx_recon import (
    TelnyxBilling,
    _micros,
    ensure_billing_groups,
    reconcile_day,
)


def test_micros():
    assert _micros('0.0085') == 8500
    assert _micros('0.00320') == 3200
    assert _micros(None) == 0
    assert _micros('1.5') == 1_500_000


@pytest.mark.asyncio
async def test_detail_records_paginates():
    seen_pages = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/v2/detail_records'
        page = int(request.url.params.get('page[number]', '1'))
        seen_pages.append(page)
        data = [{'id': f'r-{page}-{i}'} for i in range(2)]
        return httpx.Response(
            200,
            json={'data': data, 'meta': {'total_pages': 3, 'total_results': 6}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        billing = TelnyxBilling('test-key', client=client)
        records = await billing.detail_records('messaging', date(2024, 1, 1))

    assert len(records) == 6
    assert seen_pages == [1, 2, 3]


@pytest.mark.asyncio
async def test_ensure_billing_groups(session, settings):
    from tests.conftest import make_settings

    settings = make_settings(telnyx_api_key="test-telnyx-key")
    org_a = Org(id=uuid4(), name='Org A', slug='org-a')
    org_b = Org(id=uuid4(), name='Org B', slug='org-b')
    session.add_all([org_a, org_b])
    await session.commit()

    set_org_context(session, org_a.id)
    number_a = OrgNumber(
        id=uuid4(),
        org_id=org_a.id,
        e164='+12145550001',
        carrier='telnyx',
        number_type='local',
        status='active',
        is_active=True,
        provisioning={},
    )
    session.add(number_a)
    await session.commit()

    set_org_context(session, org_b.id)
    number_b = OrgNumber(
        id=uuid4(),
        org_id=org_b.id,
        e164='+12145550002',
        carrier='telnyx',
        number_type='local',
        status='active',
        is_active=True,
        provisioning={},
    )
    session.add(number_b)
    await session.commit()

    groups = []
    group_counter = 0
    post_count = 0
    list_count = 0
    patch_counts = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal group_counter, post_count, list_count
        path = request.url.path
        method = request.method

        if method == 'GET' and path == '/v2/billing_groups':
            list_count += 1
            return httpx.Response(200, json={'data': groups, 'meta': {'total_pages': 1}})

        if method == 'POST' and path == '/v2/billing_groups':
            post_count += 1
            body = json.loads(request.content)
            group_counter += 1
            new_group = {'id': f'bg-{group_counter}', 'name': body['name']}
            groups.append(new_group)
            return httpx.Response(200, json={'data': new_group})

        if method == 'GET' and path == '/v2/phone_numbers':
            e164 = request.url.params.get('filter[phone_number]')
            if e164 == '+12145550001':
                return httpx.Response(
                    200,
                    json={
                        'data': [
                            {
                                'id': 'num-a',
                                'phone_number': e164,
                                'tags': ['csaas'],
                                'billing_group_id': None,
                            }
                        ]
                    },
                )
            if e164 == '+12145550002':
                return httpx.Response(
                    200,
                    json={
                        'data': [
                            {
                                'id': 'num-b',
                                'phone_number': e164,
                                'tags': ['crm'],
                                'billing_group_id': None,
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={'data': []})

        if method == 'PATCH' and path.startswith('/v2/phone_numbers/'):
            number_id = path.rsplit('/', 1)[-1]
            patch_counts[number_id] = patch_counts.get(number_id, 0) + 1
            return httpx.Response(200, json={'data': {'id': number_id}})

        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first_counts = await ensure_billing_groups(session, settings, client=client)

        assert first_counts['groups_created'] == 2
        assert first_counts['numbers_assigned'] == 1
        assert first_counts['skipped_not_ours'] == 1
        assert patch_counts == {'num-a': 1}
        assert post_count == 2
        assert list_count == 2

        second_counts = await ensure_billing_groups(session, settings, client=client)

    assert second_counts['skipped_not_ours'] == 1
    assert post_count == 2
    assert list_count == 2
    assert patch_counts == {'num-a': 1}

    org_a_db = await session.get(Org, org_a.id)
    org_b_db = await session.get(Org, org_b.id)
    assert org_a_db.telnyx_billing_group_id == 'bg-1'
    assert org_b_db.telnyx_billing_group_id == 'bg-2'

    set_org_context(session, org_a.id)
    number_a_db = await session.get(OrgNumber, number_a.id)
    assert number_a_db.provisioning == {'telnyx_billing_group_id': 'bg-1'}


@pytest.mark.asyncio
async def test_reconcile_day(session, settings):
    from tests.conftest import make_settings

    settings = make_settings(telnyx_api_key="test-telnyx-key")
    org_a = Org(id=uuid4(), name='Recon Org', slug='recon-org')
    session.add(org_a)
    await session.commit()

    set_org_context(session, org_a.id)
    number = OrgNumber(
        id=uuid4(),
        org_id=org_a.id,
        e164='+12145550001',
        carrier='telnyx',
        number_type='local',
        status='active',
        is_active=True,
        provisioning={},
    )
    session.add(number)
    await session.commit()

    day = date(2024, 1, 1)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/v2/detail_records'
        record_type = request.url.params.get('filter[record_type]')
        if record_type == 'messaging':
            data = [
                {
                    'cli': '+12145550001',
                    'cld': '+12145550002',
                    'direction': 'outbound',
                    'cost': '0.0085',
                    'parts': 1,
                    'message_type': 'SMS',
                    'created_at': '2024-01-01T00:00:00Z',
                },
                {
                    'cli': '+12145550002',
                    'cld': '+12145550001',
                    'direction': 'inbound',
                    'cost': '0.004',
                    'parts': 1,
                    'message_type': 'SMS',
                    'created_at': '2024-01-01T00:00:00Z',
                },
                {
                    'cli': '+12145550001',
                    'cld': '+12145550002',
                    'direction': 'outbound',
                    'cost': '0.025',
                    'parts': 1,
                    'message_type': 'MMS',
                    'created_at': '2024-01-01T00:00:00Z',
                },
                {
                    'cli': '+12145559999',
                    'cld': '+12145559998',
                    'direction': 'outbound',
                    'cost': '0.01',
                    'parts': 1,
                    'message_type': 'SMS',
                    'created_at': '2024-01-01T00:00:00Z',
                },
            ]
            meta = {'total_pages': 1, 'total_results': 4, 'page_number': 1, 'page_size': 50}
        elif record_type == 'sip-trunking':
            data = [
                {
                    'cli': '+12145550001',
                    'cld': '+12145550002',
                    'direction': 'outbound',
                    'cost': '0.0064',
                    'billed_sec': 61,
                    'started_at': '2024-01-01T00:00:00Z',
                    'billing_group_id': 'ignored',
                }
            ]
            meta = {'total_pages': 1, 'total_results': 1, 'page_number': 1, 'page_size': 50}
        elif record_type == 'fax':
            data = []
            meta = {'total_pages': 1, 'total_results': 0, 'page_number': 1, 'page_size': 50}
        else:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={'data': data, 'meta': meta})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await reconcile_day(session, settings, day, client=client)
        assert first == {
            'records': 5,
            'rows': 5,
            'unmatched_cost_micros': 10000,
            'matched_cost_micros': 43900,
        }

        expected = {
            (org_a.id, 'sms', 'outbound', 1, 8500),
            (org_a.id, 'sms', 'inbound', 1, 4000),
            (org_a.id, 'mms', 'outbound', 1, 25000),
            (org_a.id, 'voice', 'outbound', 2, 6400),
            (None, 'sms', 'outbound', 1, 10000),
        }

        result = await session.execute(select(TelnyxCostDaily).where(TelnyxCostDaily.period_date == day))
        rows = result.scalars().all()
        assert {(r.org_id, r.record_type, r.direction, r.quantity, r.cost_micros) for r in rows} == expected

        second = await reconcile_day(session, settings, day, client=client)
        assert second == first

        result = await session.execute(select(TelnyxCostDaily).where(TelnyxCostDaily.period_date == day))
        rows = result.scalars().all()
        assert len(rows) == 5
        assert {(r.org_id, r.record_type, r.direction, r.quantity, r.cost_micros) for r in rows} == expected
