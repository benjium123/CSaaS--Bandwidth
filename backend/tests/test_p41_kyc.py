"""P41b/c business verification, telephony gate, suspension, ban list, selfie step-ups.

Stripe, Companies House, RDAP and business websites are never called: Stripe functions
are monkeypatched and every HTTP check runs through ``app.state.kyc_http_client``, an
httpx.MockTransport.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from app.db.base import set_org_context
from app.models import (
    ApiKey,
    FraudIdentifier,
    KycCheck,
    KycDocument,
    KycPerson,
    KycProfile,
    KycStepUp,
    Message,
    StripeEvent,
    User,
)
from app.models import Session as IdentitySession
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    FakeCarrier,
    _install,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)
from tests.fake_ai import FakeSafetyAI

OWNER_DOB = "1980-04-02"
CONTACT = "+15125550199"
OUR_NUMBER = "+15125550100"


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _website_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "rdap.org":
        return httpx.Response(
            200,
            json={"events": [{"eventAction": "registration", "eventDate": "2015-01-01T00:00:00Z"}]},
        )
    if host.endswith("acme-plumbing.example"):
        body = "<html><title>Acme Plumbing</title><body>" + ("Licensed plumbers in Austin. " * 30)
        return httpx.Response(200, text=body + "</body></html>")
    if host == "api.company-information.service.gov.uk":
        if request.url.path.endswith("/officers"):
            return httpx.Response(200, json={"items": [{"name": "SMITH, Jane Ann"}]})
        if "00000000" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={
                "company_name": "ACME PLUMBING LTD",
                "company_status": "active",
                "date_of_creation": "2012-03-01",
            },
        )
    return httpx.Response(404)


@pytest.fixture
def kyc_settings(tmp_path):
    return make_settings(
        kyc_enforced=True,
        credentials_master_key=Fernet.generate_key().decode(),
        security_data_dir=str(tmp_path / "security"),
        stripe_secret_key="sk_test_dummy",
        stripe_webhook_secret="whsec_dummy",
        public_web_url="https://console.example.test",
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def kyc_app(engine, kyc_settings, monkeypatch):
    from app.main import create_app
    from app.services import kyc_checks, stripe_client

    application = create_app(kyc_settings)
    carrier = FakeCarrier()
    _install(application, carrier)
    application.state.kyc_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_website_handler)
    )

    async def public_host(host):
        return True

    monkeypatch.setattr(kyc_checks, "_public_host", public_host)

    created: list[dict] = []

    async def fake_create(settings, *, metadata, return_url=None):
        vs_id = f"vs_{uuid.uuid4().hex[:12]}"
        created.append({"id": vs_id, "metadata": metadata, "return_url": return_url})
        return {
            "id": vs_id,
            "url": f"https://verify.stripe.com/start/{vs_id}",
            "client_secret": "x",
            "status": "requires_input",
        }

    outcomes: dict[str, dict] = {}

    async def fake_retrieve(settings, vs_id):
        return outcomes[vs_id]

    monkeypatch.setattr(stripe_client, "create_verification_session", fake_create)
    monkeypatch.setattr(stripe_client, "retrieve_verification_outcome", fake_retrieve)
    monkeypatch.setattr(
        stripe_client, "verify_webhook", lambda settings, payload, sig: json.loads(payload)
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # P43: documents are read by the safety AI - a fake one, never the real DeepSeek.
        with FakeSafetyAI().installed() as fake_ai:
            application.state.fake_ai = fake_ai
            yield c, application, carrier, created, outcomes
    await application.state.kyc_http_client.aclose()


def _write_sanctions(settings, names: list[str]) -> None:
    from app.services import sanctions

    directory = sanctions.list_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ofac_sdn.txt").write_text("\n".join(names) + "\n", encoding="utf-8")


async def _identity_event(
    client,
    outcomes,
    vs_id: str,
    *,
    status: str,
    purpose: str,
    metadata: dict,
    first="Jane",
    last="Smith",
    dob=OWNER_DOB,
    event_id=None,
):
    outcomes[vs_id] = {
        "id": vs_id,
        "status": status,
        "metadata": metadata,
        "first_name": first,
        "last_name": last,
        "dob": dob,
        "document_type": "driving_license",
        "document_country": "US",
        "error_code": None if status == "verified" else "document_unverified_other",
    }
    event = {
        "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "type": f"identity.verification_session.{status}",
        "data": {"object": {"id": vs_id, "metadata": {**metadata, "purpose": purpose}}},
    }
    return await client.post(
        "/api/v1/webhooks/stripe",
        content=json.dumps(event),
        headers={"Stripe-Signature": "t=1,v1=x"},
    )


async def _make_operator(client, session, email: str, role: str = "admin") -> str:
    from app.services import operators as operators_svc

    token = await register_and_login(client, email)
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    user.has_passkey = True
    await operators_svc.grant(session, email=email, role=role)
    live = (
        await session.execute(
            sa.select(IdentitySession)
            .where(IdentitySession.user_id == user.id)
            .order_by(IdentitySession.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    live.second_factor_at = datetime.now(timezone.utc)
    await session.commit()
    return token


async def _signup_org(client, token: str, name: str) -> dict:
    """The workspace this account works in.

    Registration now hands a self-serve signup its own workspace (auth.py::register), and
    "only one unverified workspace at a time" then makes a second POST /orgs a 409 - which
    is the rule working, not a bug. So take the workspace registration gave us and create
    one only when it gave us none. Written as a fallback rather than dropping create_org
    outright because an account can still legitimately arrive without one (an invited
    member does), and because this then survives org-on-signup changing shape again.
    """
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    if me.get("memberships"):
        return {"id": me["memberships"][0]["org_id"]}
    return await create_org(client, token, name)


async def _complete_application(
    client,
    created,
    outcomes,
    token,
    org_id,
    *,
    vertical="home_services",
    country="US",
    registration_number="EIN-12-3456789",
):
    h = auth_headers(token, org_id)
    r = await client.put(
        "/api/v1/kyc/profile/business",
        json={
            "country": country,
            "legal_name": "Acme Plumbing LLC",
            "entity_type": "llc",
            "registration_number": registration_number,
            "tax_id": "12-3456789",
            "incorporation_date": "2012-03-01",
            "registered_address": {
                "line1": "1 Main St",
                "city": "Austin",
                "region": "TX",
                "postal_code": "78701",
                "country": country,
            },
            "website": "https://acme-plumbing.example",
            "business_email": "office@acme-plumbing.example",
            "business_phone": "+15125550111",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.put(
        "/api/v1/kyc/profile/use-case",
        json={
            "description": "Appointment reminders and callbacks for plumbing customers.",
            "vertical": vertical,
            "who_you_contact": "Existing customers who booked a job",
            "list_source": "Our own booking system",
            "monthly_calls": 800,
            "monthly_texts": 2000,
            "destination_countries": ["US"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/kyc/persons",
        json={
            "role": "owner",
            "full_name": "Jane Smith",
            "email": "jane@acme-plumbing.example",
            "ownership_percent": 100,
            "is_me": True,
            "residential_address": {
                "line1": "12 Oak St",
                "city": "Austin",
                "region": "TX",
                "postal_code": "78702",
                "country": "US",
            },
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]
    r = await client.post(
        "/api/v1/kyc/documents",
        data={"kind": "proof_of_address", "person_id": person_id},
        files={"file": ("bill.pdf", _pdf_bytes(), "application/pdf")},
        headers=h,
    )
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/kyc/persons/{person_id}/verify", json={}, headers=h)
    assert r.status_code == 200, r.text
    vs = created[-1]
    assert vs["metadata"]["purpose"] == "kyc_person"
    assert vs["return_url"].startswith("https://console.example.test/")
    r = await _identity_event(
        client, outcomes, vs["id"], status="verified", purpose="kyc_person", metadata=vs["metadata"]
    )
    assert r.status_code == 204, r.text

    r = await client.post(
        "/api/v1/kyc/documents",
        data={"kind": "registration_certificate"},
        files={"file": ("cert.pdf", _pdf_bytes(), "application/pdf")},
        headers=h,
    )
    assert r.status_code == 201, r.text
    profile = (await client.get("/api/v1/kyc/profile", headers=h)).json()
    r = await client.post(
        "/api/v1/kyc/agreement",
        json={"version": profile["agreement"]["current_version"], "accept": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return person_id


# --------------------------------------------------------------------------------------
# The whole journey
# --------------------------------------------------------------------------------------
async def test_new_business_is_blocked_until_approved(kyc_app, session, kyc_settings):
    client, app, carrier, created, outcomes = kyc_app
    _write_sanctions(kyc_settings, ["IVAN BADGUY", "EVIL CORP LTD"])
    token = await register_and_login(client, "jane@acme-plumbing.example")
    org = await _signup_org(client, token, "Acme")
    h = auth_headers(token, org["id"])

    # Draft: no numbers, no texts.
    r = await client.post("/api/v1/numbers", json={"e164": OUR_NUMBER}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_not_verified"

    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "kyc_incomplete"

    await _complete_application(client, created, outcomes, token, org["id"])
    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert body["checks"]["website"]["result"] == "pass"
    # P43: Texas has no free registry feed - confirmed from the AI-read registration document.
    assert body["checks"]["registry"]["result"] == "warn"
    assert "sanctions" not in body["checks"]  # operator-only detail

    ops_token = await _make_operator(client, session, "reviewer@platform.example")
    oh = auth_headers(ops_token)
    queue = (await client.get("/api/v1/ops/queue", headers=oh)).json()
    assert [a["org_id"] for a in queue["applications"]] == [org["id"]]

    detail = (await client.get(f"/api/v1/ops/applications/{org['id']}", headers=oh)).json()
    assert detail["checks"]["sanctions"]["result"] == "pass"
    assert detail["risk"]["tier"] == "standard"
    # P43: the AI read the documents and prepared the decision; the human still decides.
    assert detail["checks"]["documents"]["result"] == "pass"
    assert detail["checks"]["ai_decision"]["detail"]["recommendation"] == "approve"
    assert detail["status"] == "submitted"

    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/registry",
        json={
            "result": "pass",
            "link": "https://opencorporates.example/acme",
            "note": "Texas SOS active",
        },
        headers=oh,
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/approve", json={"note": "ok"}, headers=oh
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    # Approved: numbers and texting work.
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_NUMBER, "carrier": "bandwidth"}, headers=h
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "from": OUR_NUMBER, "body": "Your plumber arrives at 3pm"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert len(carrier.sent) == 1

    set_org_context(session, uuid.UUID(org["id"]))
    person = (await session.execute(sa.select(KycPerson))).scalar_one()
    assert person.status == "verified"
    assert person.identity_hash and len(person.identity_hash) == 64
    audit_actions = (
        await session.execute(
            sa.text("SELECT action, actor_user_id FROM audit_log ORDER BY created_at")
        )
    ).all()
    approved = [a for a in audit_actions if a[0] == "kyc.approved"]
    assert approved and approved[0][1] is not None


async def test_identity_webhook_is_idempotent_and_never_unverifies(kyc_app, session):
    client, app, carrier, created, outcomes = kyc_app
    token = await register_and_login(client, "idem@example.com")
    org = await _signup_org(client, token, "Idem")
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/kyc/persons",
        json={"role": "owner", "full_name": "Jane Smith", "is_me": True},
        headers=h,
    )
    person_id = r.json()["id"]
    await client.post(f"/api/v1/kyc/persons/{person_id}/verify", json={}, headers=h)
    vs = created[-1]

    r = await _identity_event(
        client,
        outcomes,
        vs["id"],
        status="verified",
        purpose="kyc_person",
        metadata=vs["metadata"],
        event_id="evt_same",
    )
    assert r.status_code == 204
    # Same event id again: ignored entirely.
    r = await _identity_event(
        client,
        outcomes,
        vs["id"],
        status="requires_input",
        purpose="kyc_person",
        metadata=vs["metadata"],
        event_id="evt_same",
    )
    assert r.status_code == 204
    # A different, late event saying requires_input cannot undo verified.
    r = await _identity_event(
        client,
        outcomes,
        vs["id"],
        status="requires_input",
        purpose="kyc_person",
        metadata=vs["metadata"],
    )
    assert r.status_code == 204

    set_org_context(session, uuid.UUID(org["id"]))
    person = await session.get(KycPerson, uuid.UUID(person_id))
    assert person.status == "verified"
    assert (await session.execute(sa.select(sa.func.count(StripeEvent.id)))).scalar_one() == 2


# --------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------
async def test_documents_are_validated_and_encrypted(kyc_app, session, kyc_settings):
    client, app, *_ = kyc_app
    token = await register_and_login(client, "docs@example.com")
    org = await _signup_org(client, token, "Docs")
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/kyc/documents",
        data={"kind": "registration_certificate"},
        files={"file": ("cert.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")},
        headers=h,
    )
    assert r.status_code == 422

    pdf = _pdf_bytes()
    r = await client.post(
        "/api/v1/kyc/documents",
        data={"kind": "registration_certificate"},
        files={"file": ("../../etc/cert.pdf", pdf, "text/plain")},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "application/pdf"
    assert r.json()["filename"] == "cert.pdf"

    set_org_context(session, uuid.UUID(org["id"]))
    doc = (await session.execute(sa.select(KycDocument))).scalar_one()
    stored = await app.state.media_store.get(doc.storage_key)
    assert stored != pdf and b"%PDF" not in stored

    ops_token = await _make_operator(client, session, "docops@platform.example", role="reviewer")
    r = await client.get(
        f"/api/v1/ops/applications/{org['id']}/documents/{doc.id}", headers=auth_headers(ops_token)
    )
    assert r.status_code == 200
    assert r.content == pdf
    assert r.headers["cache-control"] == "no-store"
    # The customer's own token cannot use the operator route.
    r = await client.get(
        f"/api/v1/ops/applications/{org['id']}/documents/{doc.id}", headers=auth_headers(token)
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------------------
# Operator access
# --------------------------------------------------------------------------------------
async def test_ops_routes_need_a_named_operator(kyc_app, session):
    client, *_ = kyc_app
    from tests.conftest import TEST_PLATFORM_OPS_TOKEN

    token = await register_and_login(client, "customer@example.com")
    assert (await client.get("/api/v1/ops/queue", headers=auth_headers(token))).status_code == 403
    r = await client.get(
        "/api/v1/ops/queue", headers={"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN}
    )
    assert r.status_code == 401
    ops = await _make_operator(client, session, "named@platform.example", role="reviewer")
    assert (await client.get("/api/v1/ops/queue", headers=auth_headers(ops))).status_code == 200
    # Reviewers cannot suspend or edit the ban list.
    r = await client.post(
        "/api/v1/ops/ban-list",
        json={"kind": "email", "value": "x@y.com", "reason": "r"},
        headers=auth_headers(ops),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------------------
# Risk, sanctions, ban list
# --------------------------------------------------------------------------------------
async def test_high_risk_needs_documents_and_sanctions_match_blocks(
    kyc_app, session, kyc_settings
):
    client, app, carrier, created, outcomes = kyc_app
    # The AI can't compare the address on the bill: fine for standard risk, not for high.
    app.state.fake_ai.document = {**app.state.fake_ai.document, "address_matches": None}
    _write_sanctions(kyc_settings, ["Jane Smith"])
    token = await register_and_login(client, "risky@acme-plumbing.example")
    org = await _signup_org(client, token, "Risky")
    await _complete_application(client, created, outcomes, token, org["id"], vertical="debt_relief")
    r = await client.post("/api/v1/kyc/submit", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200, r.text

    ops = await _make_operator(client, session, "risk-ops@platform.example")
    oh = auth_headers(ops)
    detail = (await client.get(f"/api/v1/ops/applications/{org['id']}", headers=oh)).json()
    assert detail["risk"]["tier"] == "high"
    assert any("debt relief" in r for r in detail["risk"]["reasons"])
    assert detail["checks"]["sanctions"]["result"] == "fail"
    blockers = " | ".join(detail["approval_blockers"])
    # P43: no video call any more - documents must match (and fully, for high risk).
    assert "sanctions" in blockers and "document" in blockers.lower()


async def test_sanctions_lists_missing_block_approval():
    from app.services import sanctions

    settings = make_settings(security_data_dir="Z:/definitely/missing")
    result = sanctions.screen(settings, ["Jane Smith"])
    assert result.result == "error"


def test_sanctions_matching_rules(tmp_path):
    from app.services import sanctions

    settings = make_settings(security_data_dir=str(tmp_path))
    directory = sanctions.list_dir(settings)
    directory.mkdir(parents=True)
    (directory / "uk_ofsi.txt").write_text("PETROV Ivan\nGLOBAL TRADING\n", encoding="utf-8")
    assert sanctions.screen(settings, ["Ivan Petrov"]).result == "fail"
    assert sanctions.screen(settings, ["Global Trading Partners LLC"]).result == "warn"
    assert sanctions.screen(settings, ["Jane Smith"]).result == "pass"


async def test_rejected_and_banned_business_cannot_return(kyc_app, session, kyc_settings):
    client, app, carrier, created, outcomes = kyc_app
    _write_sanctions(kyc_settings, ["NOBODY LISTED"])
    token = await register_and_login(client, "scam@acme-plumbing.example")
    org = await _signup_org(client, token, "Scam One")
    await _complete_application(
        client, created, outcomes, token, org["id"], registration_number="EIN-99-0000001"
    )
    await client.post("/api/v1/kyc/submit", headers=auth_headers(token, org["id"]))

    ops = await _make_operator(client, session, "ban-ops@platform.example")
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/reject",
        json={"reason": "Scam calls reported", "ban": True},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    banned = (await session.execute(sa.select(FraudIdentifier))).scalars().all()
    kinds = {b.kind for b in banned}
    assert {"registration_number", "person", "email", "website_domain"} <= kinds
    assert all(len(b.value_hash) == 64 for b in banned)

    # Same people come back with a new account and a new company name.
    token2 = await register_and_login(client, "fresh@newname.example")
    org2 = await _signup_org(client, token2, "Totally New Co")
    await _complete_application(
        client, created, outcomes, token2, org2["id"], registration_number="EIN-99-0000001"
    )
    r = await client.post("/api/v1/kyc/submit", headers=auth_headers(token2, org2["id"]))
    assert r.status_code == 200, r.text
    detail = (
        await client.get(f"/api/v1/ops/applications/{org2['id']}", headers=auth_headers(ops))
    ).json()
    assert detail["checks"]["ban_list"]["result"] == "fail"
    assert any("ban list" in b for b in detail["approval_blockers"])


async def test_companies_house_registry_check(kyc_app, session, kyc_settings):
    from app.services import kyc_checks

    client, app, *_ = kyc_app
    kyc_settings.companies_house_api_key = type(kyc_settings.companies_house_api_key)("ch_key")
    token = await register_and_login(client, "uk@example.com")
    org = await _signup_org(client, token, "UK")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    profile = (await session.execute(sa.select(KycProfile))).scalar_one()
    profile.country = "GB"
    profile.legal_name = "Acme Plumbing Ltd"
    profile.registration_number = "12345678"
    person = KycPerson(
        id=uuid.uuid4(), org_id=org_id, role="owner", full_name="Jane Smith", status="verified"
    )
    session.add(person)
    await session.flush()
    row = await kyc_checks.check_registry(
        session, kyc_settings, profile, [person], app.state.kyc_http_client
    )
    assert row.result == "pass", row.summary

    profile.registration_number = "00000000"
    row = await kyc_checks.check_registry(
        session, kyc_settings, profile, [person], app.state.kyc_http_client
    )
    assert row.result == "fail"


# --------------------------------------------------------------------------------------
# Suspension
# --------------------------------------------------------------------------------------
async def _approved_org(client, session, email: str, name: str) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    org = await _signup_org(client, token, name)
    set_org_context(session, uuid.UUID(org["id"]))
    profile = (await session.execute(sa.select(KycProfile))).scalar_one()
    profile.status = "approved"
    user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=profile.org_id,
            role="owner",
            full_name="Jane Smith",
            user_id=user.id,
            status="verified",
            identity_hash="a" * 64,
        )
    )
    await session.commit()
    return token, org


async def test_suspension_cuts_everything_off(kyc_app, session):
    client, app, carrier, *_ = kyc_app
    from app.services import mailer

    mailer.outbox.clear()
    token, org = await _approved_org(client, session, "boss@susp.example", "Susp Co")
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_NUMBER, "carrier": "bandwidth"}, headers=h
    )
    assert r.status_code == 201, r.text
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "from": OUR_NUMBER, "body": "later", "scheduled_for": later},
        headers=h,
    )
    assert r.status_code == 201, r.text

    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    session.add(
        ApiKey(
            id=uuid.uuid4(),
            org_id=org_id,
            name="k",
            prefix="abcd1234",
            key_hash="0" * 64,
            scopes=["contacts:read"],
            status="active",
        )
    )
    await session.commit()

    ops = await _make_operator(client, session, "susp-ops@platform.example")
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/suspend",
        json={"reason": "Scam complaints", "ban": False},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended"

    # The owner's session is gone.
    assert (await client.get("/api/v1/auth/me", headers=auth_headers(token))).status_code == 401
    session.expire_all()
    set_org_context(session, org_id)
    assert (await session.execute(sa.select(ApiKey.status))).scalar_one() == "revoked"
    scheduled = (
        await session.execute(sa.select(Message).where(Message.body == "later"))
    ).scalar_one()
    assert scheduled.status == "rejected" and scheduled.error_code == "account_suspended"
    assert "boss@susp.example" in mailer.outbox[-1]["To"]

    # Signing back in works (to read why), but texting does not.
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "boss@susp.example", "password": "correct-horse-battery"},
    )
    token = r.json()["access_token"]
    r = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "from": OUR_NUMBER, "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_suspended"

    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/unsuspend",
        json={"note": "cleared"},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


async def test_scheduled_message_is_refused_at_dispatch_after_suspension(kyc_app, session):
    from app.services import telephony_access

    client, app, *_ = kyc_app
    token, org = await _approved_org(client, session, "disp@example.com", "Disp")
    org_id = uuid.UUID(org["id"])
    assert (
        await telephony_access.telephony_allowed(
            session, org_id, "sms_dispatch", settings=app.state.settings
        )
        is None
    )
    set_org_context(session, org_id)
    profile = (await session.execute(sa.select(KycProfile))).scalar_one()
    profile.status = "suspended"
    await session.commit()
    code = await telephony_access.telephony_allowed(
        session, org_id, "sms_dispatch", settings=app.state.settings
    )
    assert code == "account_suspended"


async def test_daily_limits_apply_only_when_set(kyc_app, session):
    client, app, carrier, *_ = kyc_app
    token, org = await _approved_org(client, session, "limits@example.com", "Limits")
    h = auth_headers(token, org["id"])
    assert (
        await client.post(
            "/api/v1/numbers", json={"e164": OUR_NUMBER, "carrier": "bandwidth"}, headers=h
        )
    ).status_code == 201
    set_org_context(session, uuid.UUID(org["id"]))
    profile = (await session.execute(sa.select(KycProfile))).scalar_one()
    profile.limits = {"daily_texts": 1}
    await session.commit()
    body = {"to": CONTACT, "from": OUR_NUMBER, "body": "one"}
    assert (await client.post("/api/v1/messages", json=body, headers=h)).status_code == 201
    r = await client.post("/api/v1/messages", json={**body, "body": "two"}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "daily_limit_reached"


# --------------------------------------------------------------------------------------
# Selfie step-up and privileged members
# --------------------------------------------------------------------------------------
async def test_api_key_creation_needs_a_matching_fresh_selfie(kyc_app, session):
    from app.services import kyc

    client, app, carrier, created, outcomes = kyc_app
    token, org = await _approved_org(client, session, "keys@example.com", "Keys")
    user = (
        await session.execute(sa.select(User).where(User.email == "keys@example.com"))
    ).scalar_one()
    set_org_context(session, uuid.UUID(org["id"]))
    person = (await session.execute(sa.select(KycPerson))).scalar_one()
    person.identity_hash = kyc.identity_hash("Jane", "Smith", OWNER_DOB)
    await session.commit()
    h = auth_headers(token, org["id"])
    body = {"name": "CI", "scopes": ["contacts:read"]}

    r = await client.post("/api/v1/api-keys", json=body, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "step_up_required"
    assert r.json()["error"]["kind"] == "recent_selfie"
    assert r.json()["error"]["action"] == "api_key_create"

    # Someone else's ID does not unlock it.
    r = await client.post(
        "/api/v1/kyc/step-up", json={"action": "api_key_create"}, headers=auth_headers(token)
    )
    assert r.status_code == 200, r.text
    vs = created[-1]
    await _identity_event(
        client,
        outcomes,
        vs["id"],
        status="verified",
        purpose="step_up",
        metadata=vs["metadata"],
        first="Mallory",
        last="Other",
    )
    step = (
        await client.get(f"/api/v1/kyc/step-up/{r.json()['id']}", headers=auth_headers(token))
    ).json()
    assert step["status"] == "failed"
    assert (await client.post("/api/v1/api-keys", json=body, headers=h)).status_code == 403

    # The verified owner's own selfie does - once.
    r = await client.post(
        "/api/v1/kyc/step-up", json={"action": "api_key_create"}, headers=auth_headers(token)
    )
    vs = created[-1]
    await _identity_event(
        client, outcomes, vs["id"], status="verified", purpose="step_up", metadata=vs["metadata"]
    )
    assert (await client.post("/api/v1/api-keys", json=body, headers=h)).status_code == 201
    assert (await client.post("/api/v1/api-keys", json=body, headers=h)).status_code == 403
    rows = (
        (await session.execute(sa.select(KycStepUp).where(KycStepUp.user_id == user.id)))
        .scalars()
        .all()
    )
    assert sum(1 for r in rows if r.consumed_at is not None) == 1


async def test_unverified_admin_of_approved_business_needs_id_check(kyc_app, session):
    client, app, *_ = kyc_app
    owner_token, org = await _approved_org(client, session, "owner@priv.example", "Priv")
    admin_token = await register_and_login(client, "admin@priv.example")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    from app.models import OrgMembership, Role

    admin_role = (await session.execute(sa.select(Role).where(Role.name == "admin"))).scalar_one()
    admin = (
        await session.execute(sa.select(User).where(User.email == "admin@priv.example"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=admin.id, role_id=admin_role.id)
    )
    await session.commit()

    h = auth_headers(admin_token, org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": OUR_NUMBER, "carrier": "bandwidth"}, headers=h
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "identity_verification_required"
    # Contacts are not identity-gated.
    assert (await client.get("/api/v1/contacts", headers=h)).status_code == 200
    # The owner is fine.
    r = await client.post(
        "/api/v1/numbers",
        json={"e164": OUR_NUMBER, "carrier": "bandwidth"},
        headers=auth_headers(owner_token, org["id"]),
    )
    assert r.status_code == 201, r.text


async def test_only_one_unverified_workspace_at_a_time(kyc_app):
    """The first workspace now arrives with the account itself (auth.py::register), so THAT
    is the unverified one holding the door shut. The rule under test is unchanged: while a
    workspace is unverified, a second one is refused. Asserting the signup workspace exists
    first matters - without it a 409 could just as well mean the account had no workspace
    and the rule had stopped firing for a different reason."""
    client, *_ = kyc_app
    token = await register_and_login(client, "many@example.com")
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    assert len(me["memberships"]) == 1, me
    r = await client.post("/api/v1/orgs", json={"name": "Second"}, headers=auth_headers(token))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "kyc_pending_elsewhere"


async def test_enforcement_off_keeps_legacy_behaviour(app_with_carrier):
    client, carrier, _ = app_with_carrier
    from tests.conftest import make_org_with_number

    token, org, _ = await make_org_with_number(
        client, "legacy-kyc@example.com", "Legacy", OUR_NUMBER
    )
    r = await client.post(
        "/api/v1/messages",
        json={"to": CONTACT, "from": OUR_NUMBER, "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------------------
def test_names_match():
    from app.services.kyc_checks import names_match

    assert names_match("Jane Ann Smith", "SMITH, Jane")
    assert names_match("Acme Plumbing LLC", "ACME PLUMBING")
    assert not names_match("Jane Smith", "John Smith")
    assert not names_match("Smith", "Jane Smith")


def test_identity_hash_is_order_insensitive_and_needs_dob():
    from app.services.kyc import identity_hash

    assert identity_hash("Jane", "Smith", OWNER_DOB) == identity_hash("SMITH", "jane", OWNER_DOB)
    assert identity_hash("Jane", "Smith", OWNER_DOB) != identity_hash("Jane", "Smith", "1980-04-03")
    assert identity_hash("Jane", "Smith", None) is None


def test_risk_rules():
    from app.services import kyc_risk

    settings = make_settings()
    # Pin the clock: kyc_risk works in UTC, so building the date from the LOCAL date made
    # this test fail for part of every day on any machine behind UTC (age came out 31).
    today = datetime.now(timezone.utc).date()
    profile = KycProfile(
        country="US",
        incorporation_date=today - timedelta(days=30),
        business_email="founder@gmail.com",
        use_case={
            "vertical": "crypto",
            "destination_countries": ["US", "NG"],
            "monthly_calls": 90000,
            "monthly_texts": 0,
        },
        submitted_from_flagged_login=True,
    )
    checks = {
        "website": KycCheck(
            kind="website", result="pass", summary="", detail={"domain_age_days": 20}
        )
    }
    tier, reasons = kyc_risk.evaluate(settings, profile, [], checks, today=today)
    assert tier == "high"
    text = " ".join(reasons)
    for fragment in (
        "formed 30 days",
        "20 days old",
        "free mailbox",
        "crypto",
        "NG",
        "call volume",
        "flagged sign-in",
    ):
        assert fragment in text, fragment


async def test_me_says_whether_a_member_still_needs_their_own_id_check(kyc_app, session):
    """The console could only learn this by being refused, so it is now a standing state on
    each membership - and the test's real job is to pin that the FIELD and the GATE agree at
    every step, because the failure mode of a mirrored rule is the two drifting apart.

    Three values rather than a boolean: `false` would have to mean both "already verified"
    and "this does not apply to you", which is the trap `second_factor_required` sets - a
    policy switch and a fact about a person sharing one field.
    """
    from app.models import KycPerson, KycProfile, OrgMembership, Role

    client, _app, *_ = kyc_app
    owner_token, org = await _approved_org(client, session, "owner@idv.example", "Idv")
    admin_token = await register_and_login(client, "admin@idv.example")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    admin_role = (await session.execute(sa.select(Role).where(Role.name == "admin"))).scalar_one()
    admin = (
        await session.execute(sa.select(User).where(User.email == "admin@idv.example"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=admin.id, role_id=admin_role.id)
    )
    await session.commit()

    async def state(token: str) -> str:
        body = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
        rows = [m for m in body["memberships"] if m["org_id"] == org["id"]]
        assert rows, body["memberships"]
        return rows[0]["identity_verification"]

    async def gate_refuses(token: str) -> bool:
        r = await client.post(
            "/api/v1/numbers",
            json={"e164": OUR_NUMBER, "carrier": "bandwidth"},
            headers=auth_headers(token, org["id"]),
        )
        return (
            r.status_code == 403
            and r.json()["error"]["code"] == "identity_verification_required"
        )

    # 1. An unverified admin of an APPROVED business: required, and the gate agrees.
    assert await state(admin_token) == "required"
    assert await gate_refuses(admin_token) is True

    # 2. The owner is never asked - they are the verified person on the application itself.
    assert await state(owner_token) == "not_applicable"
    assert await gate_refuses(owner_token) is False

    # 3. Before approval the application IS the gate, so the question does not apply. Same
    #    person, same role - only the workspace's status changed.
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one()
    profile.status = "in_review"
    await session.commit()
    assert await state(admin_token) == "not_applicable"
    assert await gate_refuses(admin_token) is False

    # 4. Approved again, with this member's own check passed: verified, and they are let in.
    set_org_context(session, org_id)
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one()
    profile.status = "approved"
    session.add(
        KycPerson(
            id=uuid.uuid4(),
            org_id=org_id,
            role="admin",
            user_id=admin.id,
            full_name="Admin Person",
            email="admin@idv.example",
            status="verified",
        )
    )
    await session.commit()
    assert await state(admin_token) == "verified"
    assert await gate_refuses(admin_token) is False


# ======================================================================================
# The decision emails. They have existed since d0fb1e0 and nothing covered them - grepping
# the suite for the subject lines, `_email_decision` and `DECISION_EMAILS` returns nothing.
# An untested notification path is the same family as everything else in this audit: it
# looks present, and it could have stopped working at any point without anything going red.
# `mailer.send` appends to `mailer.outbox` when `app_env == "test"`, so the means to test it
# was there the whole time.
# ======================================================================================
def _mail_body(msg) -> str:
    """The plain-text part. `mailer._build` sends multipart/alternative, so
    `get_content()` on the top-level message raises KeyError rather than returning anything."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                return part.get_content()
        return ''
    return msg.get_content()


async def _submitted_org(client, session, email: str, name: str) -> tuple[str, dict]:
    """An org sitting in `submitted`, which is where an operator decision starts."""
    token = await register_and_login(client, email)
    # Registration may or may not hand out a workspace of its own depending on where the
    # self-serve signup work has got to, and "only one unverified workspace at a time" makes
    # creating a second one a 409. Take whatever registration gave us, and create one only
    # when it gave us nothing, so this helper survives that change landing either way.
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    if me.get("memberships"):
        org = {"id": me["memberships"][0]["org_id"]}
    else:
        org = await create_org(client, token, name)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    # Scoped to THIS org rather than scalar_one() over the table: registration may create a
    # workspace of its own, and a bare select would then find two profiles and raise.
    profile = (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one()
    profile.status = "submitted"
    await session.commit()
    return token, org


async def test_operator_decisions_email_the_owners(kyc_app, session):
    """needs_info and rejected together, because the property is the SET: an operator
    decision must never be silent. Approval is the test directly below - it needed its own
    because a real approval has to clear every check first."""
    from app.services import mailer

    client, _app, *_ = kyc_app
    ops = await _make_operator(client, session, "decide@platform.example")
    oh = auth_headers(ops)

    # --- needs_info: the operator's own words reach the customer ----------------------
    _token, org = await _submitted_org(client, session, "owner@needs.example", "Needs Co")
    mailer.outbox.clear()
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/request-info",
        json={"message": "Please upload a utility bill dated in the last 90 days."},
        headers=oh,
    )
    assert r.status_code == 200, r.text
    assert len(mailer.outbox) == 1, mailer.outbox
    sent = mailer.outbox[-1]
    assert "owner@needs.example" in sent["To"]
    assert "more information" in sent["Subject"]
    assert "utility bill dated in the last 90 days" in _mail_body(sent)

    # --- rejected: the reason travels with the decision -------------------------------
    _token, org = await _submitted_org(client, session, "owner@rej.example", "Rej Co")
    mailer.outbox.clear()
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/reject",
        json={"reason": "The registry shows this company as dissolved.", "ban": False},
        headers=oh,
    )
    assert r.status_code == 200, r.text
    assert len(mailer.outbox) == 1, mailer.outbox
    sent = mailer.outbox[-1]
    assert "owner@rej.example" in sent["To"]
    assert "could not be verified" in sent["Subject"]
    assert "dissolved" in _mail_body(sent)


async def test_an_approval_email_never_carries_the_operators_private_note(
    kyc_app, session, monkeypatch
):
    """The approve route takes a `note`, and the customer must not see it. That holds today
    because the approved template has no `{message}` placeholder and `_email_decision` passes
    none - a property worth a lock rather than a coincidence worth trusting, since the other
    two templates DO interpolate and the obvious "make them consistent" refactor is the thing
    that would break it.

    `kyc_svc.approve` is stubbed because this is a test about the EMAIL. A real approval has
    to clear registry, sanctions and document checks, which the end-to-end test above already
    drives; repeating that setup here would make this test fail for reasons that have nothing
    to do with what it asserts.
    """
    from app.services import kyc as kyc_svc
    from app.services import mailer

    client, _app, *_ = kyc_app
    ops = await _make_operator(client, session, "approve@platform.example")
    _token, org = await _submitted_org(client, session, "owner@app.example", "App Co")

    async def approve_without_checks(_session, _settings, profile, _operator_id, _note):
        kyc_svc.transition(profile, "approved")

    monkeypatch.setattr(kyc_svc, "approve", approve_without_checks)
    mailer.outbox.clear()
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/approve",
        json={"note": "INTERNAL: watch this one, thin trading history"},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    assert len(mailer.outbox) == 1, mailer.outbox
    sent = mailer.outbox[-1]
    assert "owner@app.example" in sent["To"]
    assert "verified" in sent["Subject"]
    body = _mail_body(sent)
    assert "INTERNAL" not in body, "the operator's private note reached the customer"
    assert "thin trading history" not in body


async def test_a_decision_email_goes_to_owners_only(kyc_app, session):
    """`_owner_emails` joins on the owner role. A compliance decision naming a business's
    shortcomings is the owner's mail, not every member's - and the member most likely to be
    reading a shared inbox is the one a rejection may be about."""
    from app.models import OrgMembership, Role
    from app.services import mailer

    client, _app, *_ = kyc_app
    ops = await _make_operator(client, session, "owneronly@platform.example")
    _token, org = await _submitted_org(client, session, "owner@only.example", "Only Co")
    org_id = uuid.UUID(org["id"])
    await register_and_login(client, "staff@only.example")
    set_org_context(session, org_id)
    staff_role = (
        await session.execute(sa.select(Role).where(Role.name == "agent"))
    ).scalar_one()
    staff = (
        await session.execute(sa.select(User).where(User.email == "staff@only.example"))
    ).scalar_one()
    session.add(
        OrgMembership(id=uuid.uuid4(), org_id=org_id, user_id=staff.id, role_id=staff_role.id)
    )
    await session.commit()

    mailer.outbox.clear()
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/reject",
        json={"reason": "Could not verify the registered address.", "ban": False},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    recipients = mailer.outbox[-1]["To"]
    assert "owner@only.example" in recipients
    assert "staff@only.example" not in recipients


async def test_braces_in_an_operator_message_cannot_break_the_email(kyc_app, session):
    """`body.format(message=...)` formats the TEMPLATE, so the operator's text is an
    argument and not a format string - `str.format` does not recurse into substituted
    values. Reasoned through with a peer and then pinned, because "we worked out that it's
    safe" is exactly the kind of conclusion that stops being true after a refactor to
    f-strings or a template engine."""
    from app.services import mailer

    client, _app, *_ = kyc_app
    ops = await _make_operator(client, session, "braces@platform.example")
    _token, org = await _submitted_org(client, session, "owner@braces.example", "Braces Co")
    mailer.outbox.clear()
    nasty = "Send us {legal_name} and {0} and {} - literally, with the braces."
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/request-info",
        json={"message": nasty},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    assert nasty in _mail_body(mailer.outbox[-1])


async def test_a_failed_decision_email_leaves_the_decision_standing_and_says_nothing(
    kyc_app, session, monkeypatch
):
    """The decision commits BEFORE the email is attempted, and `mailer.send` reports failure
    by returning False rather than raising (mailer.py:88). So a dead SMTP host cannot undo or
    500 a decision that already succeeded - which would leave an operator staring at an error
    for work that went through, and clicking it again.

    The other half of that trade is pinned here deliberately rather than fixed: the failure is
    INVISIBLE. `_email_decision` discards the return value, so a rejected customer is never
    told and the only trace is a log line. Surfacing it is an operator-console change and
    belongs to whoever owns that screen; this test is where they will find the fact.
    """
    from app.services import mailer

    client, _app, *_ = kyc_app
    ops = await _make_operator(client, session, "silent@platform.example")
    _token, org = await _submitted_org(client, session, "owner@silent.example", "Silent Co")

    async def never_sends(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(mailer, "send", never_sends)
    r = await client.post(
        f"/api/v1/ops/applications/{org['id']}/reject",
        json={"reason": "No.", "ban": False},
        headers=auth_headers(ops),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected", "the decision must survive a mail failure"
    # The gap, stated as an assertion so it cannot be closed by accident and go unnoticed.
    # Keyed on a DELIVERY signal rather than the word "email": the application detail legitimately
    # carries a business_email field, so a bare substring check would fail for the wrong reason.
    assert not any(k in r.text for k in ("email_sent", "email_failed", "email_status")), (
        "if the response now reports delivery, the invisible-failure gap has been closed - "
        "update this test rather than deleting it"
    )
