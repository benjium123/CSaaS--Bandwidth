"""Phase 21 policy route and defaults-seeding tests."""

from __future__ import annotations

from tests.conftest import auth_headers, create_org, register_and_login


async def test_policy_get_includes_smart_routing_default_true(client):
    token = await register_and_login(client, "pol1@example.com")
    org = await create_org(client, token, "Pol1")

    r = await client.get(
        "/api/v1/routing/policy", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["smart_routing"] is True


async def test_policy_patch_smart_routing_false_requires_pinned_or_preference(client):
    token = await register_and_login(client, "pol2@example.com")
    org = await create_org(client, token, "Pol2")

    r = await client.patch(
        "/api/v1/routing/policy",
        json={"smart_routing": False},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422
    assert "Turn on Smart routing, or choose a provider to prefer first." in r.text


async def test_policy_patch_smart_routing_false_allowed_with_pinned_carrier(app_with_carrier):
    client, fake, application = app_with_carrier
    token = await register_and_login(client, "pol3@example.com")
    org = await create_org(client, token, "Pol3")

    r = await client.patch(
        "/api/v1/routing/policy",
        json={"smart_routing": False, "pinned_carrier": "bandwidth"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["smart_routing"] is False
    assert body["pinned_carrier"] == "bandwidth"


async def test_policy_patch_smart_routing_false_allowed_with_preference_in_same_request(
    app_with_carrier,
):
    client, fake, application = app_with_carrier
    token = await register_and_login(client, "pol4@example.com")
    org = await create_org(client, token, "Pol4")

    r = await client.patch(
        "/api/v1/routing/policy",
        json={"smart_routing": False, "preference": ["bandwidth"]},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["smart_routing"] is False
    assert body["preference"] == ["bandwidth"]


async def test_defaults_seed_cross_provider_failover_on_for_new_org(client):
    token = await register_and_login(client, "pol5@example.com")
    org = await create_org(client, token, "Pol5")

    r = await client.post(
        "/api/v1/orgs/current/seed-defaults",
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/api/v1/routing/policy", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allow_cross_carrier_failover"] is True
    assert body["smart_routing"] is True


async def test_existing_policy_untouched_by_reseed(client):
    token = await register_and_login(client, "pol6@example.com")
    org = await create_org(client, token, "Pol6")

    r = await client.patch(
        "/api/v1/routing/policy",
        json={"allow_cross_carrier_failover": False},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/api/v1/orgs/current/seed-defaults",
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/api/v1/routing/policy", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allow_cross_carrier_failover"] is False
