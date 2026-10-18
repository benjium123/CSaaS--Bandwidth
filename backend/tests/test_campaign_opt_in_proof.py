"""10DLC: the opt-in proof screenshot is uploaded, kept, and opened without a login."""

from __future__ import annotations

import uuid

from app.db.session import get_sessionmaker
from app.models.numbers import Campaign
from tests.conftest import auth_headers, make_org_with_number
from tests.test_numbers_registration import _approved_campaign, _org

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _draft(client, h) -> str:
    seed = await _approved_campaign(client, h, name="seed")
    r = await client.post(
        "/api/v1/registration/campaigns",
        json={"brand_id": seed["brand_id"], "name": "Draft P"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _url(cid: str) -> str:
    return f"/api/v1/registration/campaigns/{cid}/opt-in-proof"


async def test_upload_sets_a_url_served_without_auth(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "op1@example.com")
    cid = await _draft(client, h)
    r = await client.post(_url(cid), files={"file": ("p.png", PNG, "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    link = r.json()["opt_in_proof_url"]
    assert "/api/v1/media/" in link
    got = await client.get(link[link.index("/api/v1/media/"):])
    assert got.status_code == 200
    assert got.content == PNG


async def test_non_image_is_422(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "op2@example.com")
    cid = await _draft(client, h)
    r = await client.post(_url(cid), files={"file": ("p.txt", b"hello", "text/plain")}, headers=h)
    assert r.status_code == 422, r.text


async def test_active_campaign_is_409(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "op3@example.com")
    cid = await _draft(client, h)
    async with get_sessionmaker()() as session:
        session.info["org_id"] = uuid.UUID(h["X-Org-Id"])
        row = await session.get(Campaign, uuid.UUID(cid))
        row.status = "active"
        await session.commit()
    r = await client.post(_url(cid), files={"file": ("p.png", PNG, "image/png")}, headers=h)
    assert r.status_code == 409, r.text


async def test_other_orgs_campaign_is_404(app_with_carrier):
    client, _fake, _ = app_with_carrier
    _t, _o, h, _n = await _org(client, "op4@example.com")
    cid = await _draft(client, h)
    t2, o2, _n2 = await make_org_with_number(client, "op5@example.com", "Org Two", "+12145550177")
    h2 = auth_headers(t2, o2["id"])
    r = await client.post(_url(cid), files={"file": ("p.png", PNG, "image/png")}, headers=h2)
    assert r.status_code == 404, r.text
