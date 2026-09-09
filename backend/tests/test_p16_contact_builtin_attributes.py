from __future__ import annotations

from app.services.contacts import BUILTIN_CONTACT_ATTRIBUTES
from tests.conftest import auth_headers, create_org, register_and_login


async def _make_contact(client, h, name: str = "Built-in Attrs"):
    r = await client.post(
        "/api/v1/contacts",
        json={"display_name": name},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_patch_builtin_attributes_round_trips(client):
    token = await register_and_login(client, "p16-builtin-a@example.com")
    org = await create_org(client, token, "P16 Builtin Org A")
    h = auth_headers(token, org["id"])

    contact = await _make_contact(client, h)

    payload = {
        "company": "Acme Corp",
        "role": "Buyer",
        "email": "buyer@example.com",
        "address": "123 Main St",
    }
    # Sanity: the payload exercises every built-in key the service now accepts.
    assert BUILTIN_CONTACT_ATTRIBUTES == frozenset(payload)

    patched = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"attributes": payload},
        headers=h,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["attributes"] == payload

    got = await client.get(f"/api/v1/contacts/{contact['id']}", headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["attributes"] == payload


async def test_patch_builtin_attribute_non_string_is_422(client):
    token = await register_and_login(client, "p16-builtin-b@example.com")
    org = await create_org(client, token, "P16 Builtin Org B")
    h = auth_headers(token, org["id"])

    contact = await _make_contact(client, h)

    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"attributes": {"company": 12345}},
        headers=h,
    )
    assert r.status_code == 422, r.text


async def test_patch_unknown_attribute_key_is_422(client):
    token = await register_and_login(client, "p16-builtin-c@example.com")
    org = await create_org(client, token, "P16 Builtin Org C")
    h = auth_headers(token, org["id"])

    contact = await _make_contact(client, h)

    r = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"attributes": {"favorite_color": "blue"}},
        headers=h,
    )
    assert r.status_code == 422, r.text


async def test_custom_field_named_role_is_rejected(client):
    """Bugfix ledger 5.16: a CustomFieldDef sharing a key with a P16 built-in attribute
    (company, role, email, address) is now rejected outright at creation. Previously it
    was silently accepted, and it WAS enforced - validate_attributes looked up
    `defs.get(key)` first, so a colliding custom definition's own kind/options took
    precedence over the built-in and shadowed it (e.g. "role" would have to satisfy the
    custom def's `select` options instead of accepting plain text)."""
    token = await register_and_login(client, "p16-builtin-d@example.com")
    org = await create_org(client, token, "P16 Builtin Org D")
    h = auth_headers(token, org["id"])

    cf = await client.post(
        "/api/v1/custom-fields",
        json={
            "key": "role",
            "label": "Role",
            "kind": "select",
            "options": ["buyer", "seller"],
        },
        headers=h,
    )
    assert cf.status_code == 422, cf.text

    # The built-in "role" attribute still works as plain text, unaffected.
    contact = await _make_contact(client, h)
    ok = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        json={"attributes": {"role": "Buyer"}},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["attributes"] == {"role": "Buyer"}
