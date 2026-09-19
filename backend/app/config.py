"""Settings, validated at boot.

Two rules drive this module:

1. **Secrets are un-loggable by type.** Every credential field is a ``SecretStr``, whose
   repr is ``**********``. We never write a redaction helper that someone can forget to
   call — the type does it.
2. **Boot fails loudly, once.** All configuration problems are aggregated into a single
   error rather than surfacing one at a time across three restarts.

``provider_statuses()`` reports which integrations are usable and, when they are not, names
the **missing variable names** — never their values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigurationError

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ProviderStatus:
    name: str
    enabled: bool
    reason: str | None = None
    missing: list[str] = field(default_factory=list)


def _empty(v: SecretStr | str | None) -> bool:
    if v is None:
        return True
    if isinstance(v, SecretStr):
        return not v.get_secret_value().strip()
    return not str(v).strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # later-phase vars must not break boot
        case_sensitive=False,
        # Load-bearing alongside extra="ignore": a field with a validation_alias would
        # otherwise reject construction by field name AND have the keyword silently dropped,
        # so a caller passing require_2fa_privileged_users=False would quietly get the
        # default True. Fail-loud is not available here, so allow the field name.
        populate_by_name=True,
    )

    # ---------------- core ----------------
    app_env: str = "development"
    app_name: str = "csaas"
    log_level: str = "INFO"
    api_port: int = 8080
    public_base_url: str = ""
    public_web_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173"

    jwt_secret: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    credential_encryption_key: SecretStr = SecretStr("")
    #: P17: encrypts per-org provider_accounts.credentials_encrypted (Fernet key). A
    #: separate secret from credential_encryption_key on purpose - rotating one must
    #: never silently rotate the other.
    credentials_master_key: SecretStr = SecretStr("")
    #: Shared secret for platform-operator registration status callbacks. Unset means
    #: the status-changing routes answer 503 - tenants can submit, not self-attest.
    platform_ops_token: SecretStr = SecretStr("")
    jwt_expire_hours: int = 24

    # P24: when true, AI usage events debit prepaid credits. While false (default),
    # metering still runs and rows are written as shadow events without a charge.
    ai_billing_enforce: bool = False
    # P24: Stripe secret key for prepaid credit top-ups. Empty means Stripe is not
    # configured, and billing endpoints answer 503 until it is set.
    stripe_secret_key: SecretStr = SecretStr("")
    # P24: Stripe webhook signing secret used to verify that billing events came
    # from Stripe.
    stripe_webhook_secret: SecretStr = SecretStr("")
    # P24: ISO currency code used when creating Stripe price objects.
    #: P43 (audit): the rate card and every stored amount are USD by construction - there is
    #: no currency column anywhere and no conversion step - so setting this to anything else
    #: would charge USD-derived numbers in another currency. Refused at boot unless someone
    #: deliberately accepts that with the flag below.
    stripe_price_currency: str = "usd"
    #: Only set this once amounts are actually denominated in stripe_price_currency.
    allow_non_usd_pricing: bool = False
    # P24: where a customer lands after completing a top-up. Empty derives from
    # public_web_url.
    stripe_success_url: str = ""
    # P24: where a customer lands after cancelling a top-up. Empty derives from
    # public_web_url.
    stripe_cancel_url: str = ""

    # ---------------- P41 trust & safety ----------------
    #: Master switch for the mandatory second factor. When on, a user who holds a PRIVILEGED
    #: role (owner/admin - see models/rbac.is_privileged_permissions) in any org must hold an
    #: authenticator app or passkey before any route other than enrolment answers. Ordinary
    #: staff may enrol one but are not obliged to; the decision is made live from current
    #: roles in services/second_factor.py, never stored. Production refuses false (validator
    #: below); the test suite turns it off so pre-P41 tests keep exercising password login.
    #:
    #: Renamed from require_2fa_all_users when the rule stopped applying to all users. The
    #: old REQUIRE_2FA_ALL_USERS env var is still read so a live deployment does not silently
    #: fall back to the default on the next restart.
    require_2fa_privileged_users: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "REQUIRE_2FA_PRIVILEGED_USERS", "REQUIRE_2FA_ALL_USERS"
        ),
    )
    #: Telephony (texting, calling, number orders) is refused for an org whose business
    #: verification is not approved. Tests turn it off; see services/telephony_access.py.
    kyc_enforced: bool = True
    #: New orgs are created with the prepaid telephony credit gate ON - pay-as-you-go
    #: credits are the money gate for texting, calling and number rental. Platform ops can
    #: still switch a specific org off (PATCH /api/v1/platform/orgs/{id}); this only sets
    #: what a NEW org starts as. The test suite sets it False so pre-flat-pricing tests
    #: keep sending and dialling from orgs with a zero balance.
    telephony_prepaid_default: bool = True
    #: Pay-as-you-go prepaid credits have REPLACED subscription plans as the money gate for
    #: telephony: the org's credit balance is now the only thing standing between an org and
    #: outbound texting, calling or a number order. This flag must therefore stay FALSE - with
    #: it off the subscription gate is not queried at all and the credit balance is the sole
    #: money gate. The subscriptions module, its models and migration 0054 remain in the repo
    #: for billing history and possible future use; nothing here reads a subscription.
    require_subscription_for_telephony: bool = False
    #: ISO-3166 alpha-2 countries businesses may verify from, and logins are expected from.
    #: P43: US and UK only for now (Canada's registry support stays in the code; add CA here
    #: to switch it back on).
    kyc_countries: str = "US,GB"
    #: WebAuthn relying party. Empty rp_id derives from PUBLIC_WEB_URL's host; the expected
    #: origin is always PUBLIC_WEB_URL.
    webauthn_rp_id: str = ""
    #: Directory holding GeoLite2-Country.mmdb and GeoLite2-ASN.mmdb. Empty = country and
    #: datacenter checks are skipped (device checks still run).
    geolite2_dir: str = ""
    #: Tor exit list, refreshed by the sweeper into var/security/tor_exits.txt.
    tor_exit_list_url: str = "https://check.torproject.org/torbulkexitlist"
    security_data_dir: str = "var/security"
    #: A second factor proven this recently satisfies require_step_up("recent_2fa").
    step_up_2fa_minutes: int = 10
    #: A Stripe Identity selfie check this recent satisfies require_step_up("recent_selfie").
    step_up_selfie_minutes: int = 15
    #: Ordering more numbers than this in one request needs a fresh selfie.
    bulk_number_order_threshold: int = 5
    #: Optional separate signing secret when Stripe Identity events go to their own
    #: webhook endpoint; empty = STRIPE_WEBHOOK_SECRET verifies them.
    stripe_identity_webhook_secret: SecretStr = SecretStr("")
    #: UK Companies House public data API key (free). Empty = UK registry check is manual.
    companies_house_api_key: SecretStr = SecretStr("")
    #: Business documents: max upload size.
    kyc_document_max_bytes: int = 10_000_000
    #: P43: a proof of address must be dated within this many days.
    kyc_address_proof_max_days: int = 90
    #: P43: Canada's free Federal Corporation API key (api.ised-isde.canada.ca, Public Plan).
    #: Blank = Canadian registry checks fall back to the uploaded registration documents.
    ised_api_key: SecretStr = SecretStr("")
    #: Approved businesses re-verify on this cadence.
    kyc_reverify_days: int = 365
    kyc_reverify_grace_days: int = 14
    #: Which provider runs KYC person ID checks: "stripe" or "didit". Stripe is the default
    #: because it is what every existing deployment already runs; see
    #: services/identity_provider.py, which refuses any other value.
    kyc_identity_provider: str = "stripe"
    #: P44 Didit identity verification. Empty key or workflow id means the Didit provider
    #: answers 503 rather than half-starting a check.
    didit_api_key: SecretStr = SecretStr("")
    didit_workflow_id: str = ""
    #: Shared secret Didit signs its webhooks with (X-Signature-V2).
    didit_webhook_secret: SecretStr = SecretStr("")
    didit_base_url: str = "https://verification.didit.me"

    # ---------------- P42 enterprise auth ----------------
    password_min_length: int = 12
    #: Reject passwords found in known breaches (Have I Been Pwned range API; only a
    #: 5-character hash prefix is sent). Tests turn this off.
    hibp_enabled: bool = True
    hibp_api_url: str = "https://api.pwnedpasswords.com/range"
    password_reset_ttl_minutes: int = 30
    #: Failed password / second-factor attempts per account before a temporary lock.
    lockout_threshold: int = 10
    lockout_window_minutes: int = 15
    #: First lock length; doubles on each repeat within 24 h, capped at 24 h.
    lockout_base_minutes: int = 15
    #: After recovering an account by ID + selfie, step-up actions stay blocked this long.
    recovery_cooldown_hours: int = 24
    recovery_code_count: int = 10
    #: Browser sessions: signed out after this much inactivity, and after this long in total.
    #: A workspace can set stricter values in its security settings.
    session_idle_minutes: int = 30
    session_max_hours: int = 12
    #: None = Secure cookies whenever production or PUBLIC_WEB_URL is https.
    session_cookie_secure: bool | None = None
    #: Accept ``Authorization: Bearer <JWT>`` for people (pre-cookie clients). API keys are
    #: unaffected. Off by default: browsers use HttpOnly session cookies.
    auth_bearer_compat: bool = False
    #: SSO/SCIM may only enforce for, or link new people from, DNS-verified email domains.
    sso_require_verified_domain: bool = True
    #: DNS-over-HTTPS resolver used to check domain verification TXT records.
    dns_over_https_url: str = "https://cloudflare-dns.com/dns-query"
    #: Clock skew tolerated when checking SAML assertion time windows.
    saml_clock_skew_seconds: int = 120
    #: API keys expire after at most this many days (0 = no maximum).
    api_key_max_days: int = 365
    #: Owners, admins, billing members and operators must use a passkey session.
    require_passkey_for_privileged: bool = True
    passkey_grace_days: int = 14
    #: Reverse proxies in front of the app that append to X-Forwarded-For (nginx = 1).
    #: 0 = trust no header and use the socket peer address.
    trusted_proxy_count: int = 1

    # ---------------- rate limiting ----------------
    rate_limit_enabled: bool = True
    rate_limit_max_requests: int = 20
    rate_limit_window_seconds: int = 60
    #: Registration gets its OWN ceilings, because 20/minute is an API-shaped number and
    #: account creation is not an API call. Under invite-only nobody cared: a valid token was
    #: the real limit, and the request limit was a formality. Public self-serve signup removes
    #: the token, and then this is the only thing standing between one address and 1,200
    #: accounts an hour.
    #:
    #: TWO windows rather than one, because they stop different things. The burst limit stops a
    #: script; the hourly limit stops a patient script. A single number cannot do both without
    #: being either useless against volume or hostile to a shared office IP.
    #:
    #: Deliberately not tighter: `enforce_rate_limit` runs BEFORE password-policy validation,
    #: so a person fumbling the password rules spends this budget on attempts that created no
    #: account. Five a minute leaves room for that; three would turn a rejected password into a
    #: lockout. The hourly figure is sized for NAT - a coworking space or a university can put
    #: many genuine signups behind one address - while still being ~60x tighter than the
    #: inherited default.
    registration_ip_burst_max: int = 5
    registration_ip_burst_seconds: int = 60
    registration_ip_hourly_max: int = 20
    registration_ip_hourly_seconds: int = 3600

    # ---------------- datastores ----------------
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    database_url_sync: str = ""
    db_pool_size: int = 20
    db_max_overflow: int = 10
    redis_url: str = ""

    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")

    # ---------------- carriers ----------------
    # TRI-STATE (phase-9b DR-1): None/unset = AUTO - the carrier is live iff its required
    # credentials are present. true/false = explicit override; false is the kill-switch.
    # Rationale: requiring keys AND a flag is a silent-failure generator - you paste a key,
    # nothing happens, and nothing tells you why.
    bandwidth_enabled: bool | None = None
    bandwidth_account_id: str = ""
    bandwidth_api_username: str = ""
    bandwidth_api_password: SecretStr = SecretStr("")
    bandwidth_messaging_application_id: str = ""
    bandwidth_voice_application_id: str = ""
    # P18: Bandwidth Site (sub-account) id used for number orders. Optional.
    bandwidth_site_id: str = ""
    #: "oauth2" for a dashboard Client ID / Client Secret (current Bandwidth model),
    #: "basic" for a legacy API-user username/password pair.
    bandwidth_auth_mode: str = "oauth2"
    bandwidth_webhook_username: str = ""
    bandwidth_webhook_password: SecretStr = SecretStr("")
    bandwidth_default_number: str = ""

    telnyx_enabled: bool | None = None
    telnyx_api_key: SecretStr = SecretStr("")
    telnyx_public_key: SecretStr = SecretStr("")
    telnyx_messaging_profile_id: str = ""
    telnyx_voice_connection_id: str = ""
    telnyx_default_number: str = ""

    # ---- P37 managed telephony (one Telnyx managed sub-account per org) ---------------
    #: Dark by default: every /api/v1/telephony route answers 503 while this is false, so
    #: the feature ships unreachable until the operator flips it after Telnyx approves
    #: Managed Accounts on the platform's master account.
    telephony_managed_enabled: bool = False
    #: The PLATFORM master account's API key - the one that owns every managed
    #: sub-account. Never per org, never logged, never returned by any route.
    telnyx_master_api_key: SecretStr = SecretStr("")

    # ---- LiveKit media plane (D17: one media plane for softphone + AI agent) ----------
    #: e.g. ws://127.0.0.1:7880 self-hosted; the browser needs the wss:// public form.
    livekit_url: str = ""
    #: Public URL the softphone connects to (behind nginx TLS); falls back to livekit_url.
    livekit_public_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: SecretStr = SecretStr("")
    #: SIP trunk id configured in livekit-sip for OUTBOUND calls (lk sip outbound create).
    livekit_sip_outbound_trunk_id: str = ""
    #: P40: a second outbound trunk that reaches the PSTN through a SignalWire SIP domain
    #: app. Calls from a signalwire number use it; everything else keeps the Telnyx trunk.
    livekit_sip_signalwire_trunk_id: str = ""
    #: P42: INBOUND trunk ids, kept in sync (`numbers`) by `voice_plane/trunk_sync.py`
    #: instead of manual trunk recreation. Empty = that carrier's trunk is not ours to
    #: sync (e.g. the CRM's own shared LiveKit instance).
    livekit_sip_telnyx_inbound_trunk_id: str = ""
    livekit_sip_signalwire_inbound_trunk_id: str = ""

    signalwire_enabled: bool | None = None
    signalwire_project_id: str = ""
    signalwire_api_token: SecretStr = SecretStr("")
    #: e.g. "yourspace.signalwire.com"
    signalwire_space_url: str = ""
    #: The URL we REGISTERED with SignalWire. Twilio-compatible signatures cover the URL,
    #: and it must not be reconstructed from an attacker-controllable Host header.
    signalwire_webhook_url: str = ""
    signalwire_default_number: str = ""

    twilio_enabled: bool | None = None
    #: Account SID (starts "AC"). Console -> Account Info.
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    #: Optional Messaging Service SID ("MG..."); when set, Twilio picks the sender.
    twilio_messaging_service_sid: str = ""
    #: The URL we REGISTERED with Twilio. The signature covers the URL, and it must never
    #: be rebuilt from an attacker-controllable Host header.
    twilio_webhook_url: str = ""
    twilio_default_number: str = ""

    plivo_enabled: bool | None = None
    plivo_auth_id: str = ""
    plivo_auth_token: SecretStr = SecretStr("")
    #: Optional Powerpack UUID for pooled sending.
    plivo_powerpack_uuid: str = ""
    #: Registered webhook URL - the V3 signature covers it (same reasoning as Twilio).
    plivo_webhook_url: str = ""
    plivo_default_number: str = ""

    #: 1.1: `POST /numbers` (manual add) verifies ownership through the resolved
    #: carrier's `lookup_owned_number` before accepting an e164 - otherwise any org could
    #: claim a number another tenant already owns and hijack its inbound traffic. A
    #: carrier that CANNOT verify (returns None - e.g. its API is unreachable, or it does
    #: not support a lookup) is refused by default; this flag is the explicit, off-by-
    #: default opt-in to accept an unverifiable number anyway.
    allow_unverified_number_add: bool = False

    #: 4.29/1.1: preferred carrier name when an org has more than one DB-backed provider
    #: account active and none is explicitly named - empty means "no preference" (the
    #: existing bandwidth/telnyx/signalwire fallback order applies).
    primary_provider: str = ""

    #: Refuse to send from a number we hold no registration for. Off by default because a
    #: number registered directly at the carrier (Bandwidth's trial number, for one) is
    #: perfectly legitimate and we should not block it on an assumption. Note the
    #: direction: this flag can only make enforcement STRICTER. There is no flag that
    #: loosens it, and none that lets a deployment claim a registration it does not hold.
    require_number_registration: bool = False

    #: TEST/DEV ONLY. Registration is invite-only in production and the validator below
    #: REFUSES this flag when APP_ENV=production, so it cannot be the reason a live
    #: instance is open. It exists because the test suite legitimately creates many users,
    #: and the alternative - a conftest that inserts users behind the API - would stop
    #: exercising the real registration path in every one of those tests.
    allow_open_registration: bool = False
    #: P43: public signup takes work addresses only. On by default so that turning
    #: ALLOW_OPEN_REGISTRATION on never quietly opens the door to consumer mailboxes too -
    #: the two decisions should have to be made separately. Only applies to registrations
    #: with no invite; an invited colleague joins on whatever address their employer used.
    require_business_email: bool = True
    #: Comma-separated extra consumer domains, so a newly popular provider needs no deploy.
    extra_consumer_email_domains: str = ""

    # DEV/DEMO ONLY. See providers/loopback.py. The validator below refuses it in
    # production and refuses it alongside a real carrier.
    loopback_carrier_enabled: bool = False

    # ---------------- AI ----------------
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    #: P43: DeepSeek endpoint and the model the platform's safety AI uses (KYC document
    #: reading and decision packs, text and call monitoring). Platform-paid, never BYOK.
    deepseek_base_url: str = "https://api.deepseek.com"
    ai_guard_model: str = "deepseek-flash"
    #: Off = every AI check reports "unavailable" and the fail-safe rules apply.
    ai_guard_enabled: bool = True
    ai_guard_timeout_seconds: float = 20.0
    #: P43 traffic monitoring. Off = texts/calls are not screened and nothing is paused.
    monitor_enforced: bool = True
    #: The pre-send text check waits at most this long for the AI.
    monitor_text_ai_timeout_seconds: float = 6.0
    #: New accounts get the strictest treatment (AI outage = hold; every call reviewed).
    monitor_new_account_days: int = 60
    #: Share of calls reviewed for established, normal-level accounts.
    monitor_call_sample_percent: int = 20
    #: Held texts not cleared by then are rejected.
    monitor_hold_max_hours: int = 24
    #: Risk score thresholds (sum of signal weights over the window).
    monitor_watch_score: int = 30
    monitor_restrict_score: int = 60
    monitor_pause_score: int = 100
    #: OPERATOR POLICY: when off (the default), the monitor DETECTS but never restricts.
    #: Escalation to `watch` still happens automatically because it throttles nothing - it
    #: only makes calls get reviewed. Anything at `restricted` or above becomes a
    #: recommendation an operator applies or rejects. Turning this ON restores automatic
    #: enforcement, which stops a scam campaign sooner at the cost of acting on a paying
    #: customer without a human in the loop.
    monitor_auto_action: bool = False
    #: Window an ON-DEMAND thorough review looks back over, versus the 24h the hourly sweep
    #: uses. An operator asking for a full review wants history, not the last day.
    monitor_review_window_days: int = 30
    #: Cap on AI calls a single on-demand review may make, so one click cannot spend an
    #: unbounded amount on one account.
    monitor_review_max_ai_calls: int = 25
    monitor_signal_window_days: int = 30
    #: Daily caps while restricted (when the business has no lower limit of its own).
    monitor_restricted_daily_texts: int = 200
    monitor_restricted_daily_calls: int = 100
    groq_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")

    stt_provider: str = "deepgram"
    deepgram_api_key: SecretStr = SecretStr("")
    assemblyai_api_key: SecretStr = SecretStr("")

    tts_provider: str = "elevenlabs"
    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_voice_id: str = ""
    cartesia_api_key: SecretStr = SecretStr("")

    # P23a: when true, GET /api/v1/agent/config/{call_id} hands the worker the
    # DECRYPTED per-org AI keys. Off until the voice worker can accept them.
    ai_per_org_keys: bool = False

    # P23b: the LiveKit agent_name the assistant worker registers under. The dispatch
    # falls back to "ai-agent" when unset, which is what deploy/livekit/README documents.
    ai_agent_name: str = "ai-agent"
    #: P43: the silent call-monitor listener worker (agents/call_monitor.py).
    monitor_agent_name: str = "call-monitor"

    # ---------------- media / storage ----------------
    media_store_backend: str = "local"   # local | memory | s3 (s3 raises until P5)
    media_local_root: str = "var/media"
    media_retention_days: int = 0        # 0 = never expire
    sweeper_enabled: bool = True
    sweeper_interval_seconds: int = 60

    # ---------------- ops ----------------
    sentry_dsn: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_port: int = 587
    #: From address for security alerts and verification emails.
    smtp_from: str = ""

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def kyc_country_list(self) -> list[str]:
        return [c.strip().upper() for c in self.kyc_countries.split(",") if c.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []

        if _empty(self.jwt_secret):
            problems.append("JWT_SECRET is required (generate: openssl rand -hex 32)")
        if self.stripe_price_currency.strip().lower() != "usd" and not self.allow_non_usd_pricing:
            problems.append(
                f"STRIPE_PRICE_CURRENCY is {self.stripe_price_currency!r} but every rate and "
                "balance in this system is denominated in USD with no conversion step, so "
                "customers would be charged USD amounts labelled as that currency. Set "
                "ALLOW_NON_USD_PRICING=true only once the rate card is actually in it."
            )
        if _empty(self.session_secret):
            problems.append("SESSION_SECRET is required (generate: openssl rand -hex 32)")

        if self.is_production:
            if _empty(self.credential_encryption_key):
                problems.append("CREDENTIAL_ENCRYPTION_KEY is required when APP_ENV=production")
            else:
                try:
                    from cryptography.fernet import Fernet

                    Fernet(self.credential_encryption_key.get_secret_value().encode())
                except Exception:
                    problems.append("CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key")

            if not self.public_base_url.startswith("https://"):
                problems.append("PUBLIC_BASE_URL must be https:// in production")
            if "example.com" in self.public_base_url:
                problems.append("PUBLIC_BASE_URL still points at the example placeholder")
            if not self.public_web_url.startswith("https://"):
                problems.append("PUBLIC_WEB_URL must be https:// in production")

            if _empty(self.credentials_master_key):
                problems.append("CREDENTIALS_MASTER_KEY is required when APP_ENV=production")
            else:
                if len(self.credentials_master_key.get_secret_value().encode()) < 32:
                    problems.append(
                        "CREDENTIALS_MASTER_KEY is too weak - use at least 32 bytes"
                    )

            if "*" in self.cors_origin_list:
                problems.append("CORS cannot use '*' with credentials in production")
            if "csaas:csaas@" in self.database_url:
                problems.append("DATABASE_URL still uses the default development credentials")
            # OPEN REGISTRATION: a requirement, not a refusal.
            #
            # This used to refuse the boot outright. That was right while the product was
            # invite-only, and deleting it now that public self-serve signup is the business
            # model would be wrong in a specific way: the refusal was not hygiene, it was the
            # thing that made the flag safe to leave in the codebase. Delete it and the only
            # surviving record of the decision is an env var somebody flipped once, on a day
            # nobody remembers, with no statement anywhere of what that flip took away.
            #
            # An invite token was doing three jobs beyond letting someone in. It proved
            # somebody already trusted at this company chose to invite this person. It proved
            # the address was reachable, because the token arrived there. And it capped account
            # creation at the rate humans issue invitations, which made the request rate limit a
            # formality. Public signup removes all three at once, so the conditions below are
            # what has to be true before the boot is allowed to proceed without them.
            #
            # WHAT THIS CANNOT PROMISE. It checks that controls are CONFIGURED, not that they
            # are effective - a boot check has no other reach. `require_business_email` in
            # particular only applies to registrations with no invite and only past first-run,
            # so "the setting is on" is a weaker claim than "every registration is filtered",
            # and the message below says only the weaker thing on purpose. A domain list also
            # answers "clean" for every domain it has not heard of, so it filters volume and
            # establishes nothing about who signed up. The control that actually holds is the
            # operator's rule that nothing can be purchased before KYC approval - which is why
            # KYC_ENFORCED is on this list rather than being assumed.
            if self.allow_open_registration:
                missing_controls = []
                if not self.require_business_email:
                    missing_controls.append(
                        "REQUIRE_BUSINESS_EMAIL must be true - with open registration it is "
                        "the only filter on who may create an account"
                    )
                if not self.kyc_enforced:
                    missing_controls.append(
                        "KYC_ENFORCED must be true - open registration with verification off "
                        "means anyone who signs up can reach telephony with nothing checked"
                    )
                if not self.redis_url.strip():
                    missing_controls.append(
                        "REDIS_URL must be set - without it every rate limit falls back to a "
                        "per-process counter, so the registration ceiling silently multiplies "
                        "by the worker count and nothing reports that it happened"
                    )
                if missing_controls:
                    problems.append(
                        "ALLOW_OPEN_REGISTRATION is on, which removes invite-only signup. "
                        "It is permitted in production only alongside the controls an invite "
                        "was providing: " + "; ".join(missing_controls)
                    )
            if not self.redis_url.strip():
                problems.append(
                    "REDIS_URL is required in production - rate limits and session revocation "
                    "must be shared by every worker"
                )
            if not self.require_2fa_privileged_users:
                problems.append(
                    "REQUIRE_2FA_PRIVILEGED_USERS (formerly REQUIRE_2FA_ALL_USERS) must be "
                    "true in production - owners, admins and anyone who can change access "
                    "or billing needs an authenticator app or passkey. Ordinary staff are "
                    "not obliged to hold one."
                )
            if self.loopback_carrier_enabled:
                problems.append(
                    "LOOPBACK_CARRIER_ENABLED must be false in production - it is a fake "
                    "carrier that never touches the PSTN"
                )
            if self.carrier_live("bandwidth") and (
                _empty(self.bandwidth_webhook_username)
                or _empty(self.bandwidth_webhook_password)
            ):
                problems.append(
                    "BANDWIDTH_WEBHOOK_USERNAME / BANDWIDTH_WEBHOOK_PASSWORD are required "
                    "when Bandwidth is enabled - voice webhooks fail closed without them"
                )

        if self.loopback_carrier_enabled:
            # Loopback is a FAKE carrier. It must never coexist with a real one, and the
            # test is INTENT, not merely capability: an explicit *_ENABLED=true says the
            # operator means to send for real, even if the credentials are not there yet.
            # (Under tri-state, checking only carrier_live() would let that contradiction
            # through the moment the keys were absent - which is exactly when a confused
            # deployment is most likely.)
            contradicting = [
                name
                for name in self.carrier_requirements()
                if self.carrier_live(name) or self.carrier_flag(name) is True
            ]
            if contradicting:
                problems.append(
                    "LOOPBACK_CARRIER_ENABLED is true alongside "
                    + ", ".join(f"{n.upper()}_ENABLED" for n in contradicting)
                    + " - which carrier should send? Disable one."
                )

        if problems:
            raise ConfigurationError(
                "Configuration is invalid:\n  - " + "\n  - ".join(problems)
            )
        return self

    # ------------------------------------------------------------------
    #: Every carrier and the credentials it cannot run without. This map IS the
    #: "bring your own key" contract: adding a carrier here makes it discoverable by the
    #: status endpoint, the console and the tri-state auto-enable, in one place.
    def carrier_requirements(self) -> dict[str, dict[str, object]]:
        return {
            "bandwidth": {
                "BANDWIDTH_ACCOUNT_ID": self.bandwidth_account_id,
                "BANDWIDTH_API_USERNAME": self.bandwidth_api_username,
                "BANDWIDTH_API_PASSWORD": self.bandwidth_api_password,
            },
            "telnyx": {"TELNYX_API_KEY": self.telnyx_api_key},
            "twilio": {
                "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
                "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            },
            "plivo": {
                "PLIVO_AUTH_ID": self.plivo_auth_id,
                "PLIVO_AUTH_TOKEN": self.plivo_auth_token,
            },
            "signalwire": {
                "SIGNALWIRE_PROJECT_ID": self.signalwire_project_id,
                "SIGNALWIRE_API_TOKEN": self.signalwire_api_token,
                "SIGNALWIRE_SPACE_URL": self.signalwire_space_url,
            },
        }

    def carrier_flag(self, name: str) -> bool | None:
        return getattr(self, f"{name}_enabled", None)

    def carrier_live(self, name: str) -> bool:
        """Effective enablement. Read this, never the raw flag - the flag alone cannot
        answer the question now that unset means auto."""
        flag = self.carrier_flag(name)
        if flag is False:
            return False
        required = self.carrier_requirements().get(name, {})
        return not [k for k, v in required.items() if _empty(v)]  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    def provider_statuses(self) -> list[ProviderStatus]:
        """One status per integration. Names missing variables, never values."""
        out: list[ProviderStatus] = []

        def carrier(name: str) -> None:
            """Tri-state (phase-9b DR-1). flag None = AUTO: live iff credentials present."""
            flag = self.carrier_flag(name)
            required = self.carrier_requirements()[name]
            missing = [k for k, v in required.items() if _empty(v)]  # type: ignore[arg-type]

            if flag is False:
                out.append(
                    ProviderStatus(name, False, reason=f"{name.upper()}_ENABLED is false")
                )
            elif missing and flag is True:
                out.append(
                    ProviderStatus(
                        name,
                        False,
                        reason=f"{name.upper()}_ENABLED=true but missing: {', '.join(missing)}",
                        missing=missing,
                    )
                )
            elif missing:
                out.append(
                    ProviderStatus(
                        name,
                        False,
                        reason=f"not configured; add {', '.join(missing)} to enable",
                        missing=missing,
                    )
                )
            else:
                out.append(ProviderStatus(name, True))

        def keyed(name: str, required: dict[str, object], note: str = "") -> None:
            missing = [k for k, v in required.items() if _empty(v)]  # type: ignore[arg-type]
            if missing:
                reason = f"not configured; missing: {', '.join(missing)}"
                out.append(ProviderStatus(name, False, reason=reason + note, missing=missing))
            else:
                out.append(ProviderStatus(name, True))

        for carrier_name in self.carrier_requirements():
            carrier(carrier_name)

        keyed("anthropic", {"ANTHROPIC_API_KEY": self.anthropic_api_key})
        keyed("openai", {"OPENAI_API_KEY": self.openai_api_key})
        keyed("deepseek", {"DEEPSEEK_API_KEY": self.deepseek_api_key})
        keyed("groq", {"GROQ_API_KEY": self.groq_api_key})
        keyed("google", {"GOOGLE_API_KEY": self.google_api_key})
        keyed("deepgram", {"DEEPGRAM_API_KEY": self.deepgram_api_key})
        keyed("assemblyai", {"ASSEMBLYAI_API_KEY": self.assemblyai_api_key})
        keyed(
            "elevenlabs",
            {
                "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
                "ELEVENLABS_VOICE_ID": self.elevenlabs_voice_id,
            },
        )
        keyed("cartesia", {"CARTESIA_API_KEY": self.cartesia_api_key})
        keyed(
            "livekit",
            {
                "LIVEKIT_URL": self.livekit_url,
                "LIVEKIT_API_SECRET": self.livekit_api_secret,
                # Either trunk makes calling possible.
                "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": self.livekit_sip_outbound_trunk_id
                or self.livekit_sip_signalwire_trunk_id,
            },
        )
        keyed(
            "s3",
            {
                "S3_BUCKET": self.s3_bucket,
                "S3_ACCESS_KEY_ID": self.s3_access_key_id,
                "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            },
        )
        keyed("stripe", {"STRIPE_SECRET_KEY": self.stripe_secret_key,
                         "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret})
        keyed("didit", {"DIDIT_API_KEY": self.didit_api_key,
                        "DIDIT_WORKFLOW_ID": self.didit_workflow_id},
              note=" (only needed when KYC_IDENTITY_PROVIDER=didit)")
        keyed("companies_house", {"COMPANIES_HOUSE_API_KEY": self.companies_house_api_key},
              note=" (UK registry checks fall back to manual)")
        keyed("geolite2", {"GEOLITE2_DIR": self.geolite2_dir},
              note=" (login country/datacenter checks are skipped)")
        keyed("redis", {"REDIS_URL": self.redis_url})
        keyed("smtp", {"SMTP_HOST": self.smtp_host})
        keyed("sentry", {"SENTRY_DSN": self.sentry_dsn})

        # Deliberately hard-coded disabled: we hold no DNC registry subscription and no
        # OSS library exists. There is NO settings flag that could claim otherwise -
        # making this configurable would let a deployment believe it was scrubbing.
        out.append(
            ProviderStatus(
                "federal_dnc",
                False,
                reason=(
                    "no registry subscription - numbers are NOT scrubbed against the "
                    "federal DNC"
                ),
            )
        )
        return out


#: P43: names for the verification countries, used in messages and the AI reviewer's brief.
COUNTRY_NAMES = {"US": "the United States", "GB": "the United Kingdom", "CA": "Canada"}


def countries_phrase(codes: list[str]) -> str:
    names = [COUNTRY_NAMES.get(c, c) for c in codes]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)


#: P41: the settings the running app was built with. Services deep in call paths that
#: have no request (sweeper ticks, the dialer, dispatch re-checks) read enforcement flags
#: from here instead of re-reading the environment. Set by main.create_app.
_active_settings: Settings | None = None


def set_active_settings(settings: Settings) -> None:
    global _active_settings
    _active_settings = settings


def get_active_settings() -> Settings:
    return _active_settings if _active_settings is not None else load_settings()
