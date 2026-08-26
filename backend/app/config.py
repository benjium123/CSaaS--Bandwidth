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

from pydantic import SecretStr, model_validator
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
    jwt_expire_hours: int = 24

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
    bandwidth_enabled: bool = False
    bandwidth_account_id: str = ""
    bandwidth_api_username: str = ""
    bandwidth_api_password: SecretStr = SecretStr("")
    bandwidth_messaging_application_id: str = ""
    bandwidth_voice_application_id: str = ""
    bandwidth_webhook_username: str = ""
    bandwidth_webhook_password: SecretStr = SecretStr("")
    bandwidth_default_number: str = ""

    telnyx_enabled: bool = False
    telnyx_api_key: SecretStr = SecretStr("")
    telnyx_public_key: SecretStr = SecretStr("")
    telnyx_messaging_profile_id: str = ""
    telnyx_voice_connection_id: str = ""
    telnyx_default_number: str = ""

    # DEV/DEMO ONLY. See providers/loopback.py. The validator below refuses it in
    # production and refuses it alongside a real carrier.
    loopback_carrier_enabled: bool = False

    # ---------------- AI ----------------
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")

    stt_provider: str = "deepgram"
    deepgram_api_key: SecretStr = SecretStr("")
    assemblyai_api_key: SecretStr = SecretStr("")

    tts_provider: str = "elevenlabs"
    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_voice_id: str = ""
    cartesia_api_key: SecretStr = SecretStr("")

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

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []

        if _empty(self.jwt_secret):
            problems.append("JWT_SECRET is required (generate: openssl rand -hex 32)")
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
            if "csaas:csaas@" in self.database_url:
                problems.append("DATABASE_URL still uses the default development credentials")
            if self.loopback_carrier_enabled:
                problems.append(
                    "LOOPBACK_CARRIER_ENABLED must be false in production - it is a fake "
                    "carrier that never touches the PSTN"
                )

        if self.loopback_carrier_enabled and self.bandwidth_enabled:
            problems.append(
                "LOOPBACK_CARRIER_ENABLED and BANDWIDTH_ENABLED are both true - which "
                "carrier should send? Disable one."
            )

        if problems:
            raise ConfigurationError(
                "Configuration is invalid:\n  - " + "\n  - ".join(problems)
            )
        return self

    # ------------------------------------------------------------------
    def provider_statuses(self) -> list[ProviderStatus]:
        """One status per integration. Names missing variables, never values."""
        out: list[ProviderStatus] = []

        def flagged(name: str, flag: bool, required: dict[str, object]) -> None:
            if not flag:
                out.append(
                    ProviderStatus(name, False, reason=f"{name.upper()}_ENABLED is false")
                )
                return
            missing = [k for k, v in required.items() if _empty(v)]  # type: ignore[arg-type]
            if missing:
                out.append(
                    ProviderStatus(
                        name,
                        False,
                        reason=f"{name.upper()}_ENABLED=true but missing: {', '.join(missing)}",
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

        flagged(
            "bandwidth",
            self.bandwidth_enabled,
            {
                "BANDWIDTH_ACCOUNT_ID": self.bandwidth_account_id,
                "BANDWIDTH_API_USERNAME": self.bandwidth_api_username,
                "BANDWIDTH_API_PASSWORD": self.bandwidth_api_password,
            },
        )
        flagged(
            "telnyx",
            self.telnyx_enabled,
            {"TELNYX_API_KEY": self.telnyx_api_key},
        )

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
            "s3",
            {
                "S3_BUCKET": self.s3_bucket,
                "S3_ACCESS_KEY_ID": self.s3_access_key_id,
                "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            },
        )
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


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
