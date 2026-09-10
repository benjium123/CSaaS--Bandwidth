"""P23: per-org AI provider credentials (LLM / STT / TTS), mirroring provider_accounts.

Separate table from `provider_accounts` on purpose: the carrier registry (P17) must not
learn about AI providers, and the two have different kinds, probes, and billing rules.
Credentials are Fernet-encrypted with the same master key (services/credentials.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

AI_PROVIDER_KINDS: tuple[str, ...] = ("llm", "stt", "tts")

#: kind -> providers the platform knows how to call (config.py already carries platform-wide
#: env keys for each of these).
AI_PROVIDERS_BY_KIND: dict[str, tuple[str, ...]] = {
    "llm": ("openai", "anthropic", "deepseek", "groq", "google"),
    "stt": ("deepgram", "assemblyai"),
    "tts": ("elevenlabs", "cartesia"),
}

AI_PROVIDER_ACCOUNT_STATUSES: tuple[str, ...] = ("unverified", "active", "failed", "disabled")

#: Field catalogue per provider. ``True`` = secret (write-only, never echoed).
AI_PROVIDER_CREDENTIAL_FIELDS: dict[str, dict[str, bool]] = {
    "openai": {"api_key": True},
    "anthropic": {"api_key": True},
    "deepseek": {"api_key": True},
    "groq": {"api_key": True},
    "google": {"api_key": True},
    "deepgram": {"api_key": True},
    "assemblyai": {"api_key": True},
    "elevenlabs": {"api_key": True, "default_voice_id": False},
    "cartesia": {"api_key": True},
}

AI_KEY_MODES: tuple[str, ...] = ("platform", "byok")


class AiProviderAccount(Base, TenantScoped, TimestampMixin):
    __tablename__ = "ai_provider_accounts"
    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "kind", "provider", "label", name="uq_ai_provider_accounts_org_kind_provider"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(127), nullable=False, default="")
    credentials_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_probe_detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<AiProviderAccount {self.kind}/{self.provider} {self.status}>"
