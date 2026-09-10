"""P24 money models (Fable-owned): AI usage events, prepaid credit ledger, payment methods.

Rules that the schema enforces and services/credits.py must respect:
- `credit_ledger` is APPEND-ONLY. Never UPDATE or DELETE a row. Balance = the newest row's
  balance_after_micros; a nightly check asserts SUM(amount_micros) == that value.
- A (org, entry_type, reference) is charged once: Stripe intent ids, usage event ids and
  reserve tokens are the references, so replays cannot double-credit or double-debit.
- `ai_usage_events` is append-only and idempotent on idempotency_key (worker batches retry).
- Money is integer micros (1_000_000 = $1.00), never floats.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

AI_USAGE_KINDS: tuple[str, ...] = ("llm", "stt", "tts", "voice")
AI_USAGE_SOURCES: tuple[str, ...] = ("worker", "simulate", "sms_agent")
AI_USAGE_METRICS: tuple[str, ...] = (
    "ai_voice_seconds",
    "stt_seconds",
    "tts_characters",
    "llm_tokens_in",
    "llm_tokens_out",
)
LEDGER_ENTRY_TYPES: tuple[str, ...] = (
    "topup",
    "usage",
    "adjustment",
    "refund",
    "reserve",
    "release",
)
RATE_SCOPES: tuple[str, ...] = ("traffic", "ai")
#: Platform default margin on AI usage when the org has no override: 30%.
DEFAULT_AI_MARKUP_BPS = 3000


class AiUsageEvent(Base, TenantScoped, TimestampMixin):
    __tablename__ = "ai_usage_events"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_ai_usage_events_idempotency"),
        sa.Index("ix_ai_usage_events_org_occurred", "org_id", "occurred_at"),
        sa.Index("ix_ai_usage_events_org_call", "org_id", "call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("message_threads.id", ondelete="SET NULL"), nullable=True
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    #: What we pay the provider for this event (from provider_rates, scope 'ai').
    cost_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: What the customer pays: cost x (1 + markup) in platform mode; the per-minute platform
    #: fee for BYOK voice; 0 for BYOK tokens/characters.
    price_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)


class CreditLedgerEntry(Base, TenantScoped, TimestampMixin):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "entry_type", "reference", name="uq_credit_ledger_org_type_ref"
        ),
        sa.Index("ix_credit_ledger_org_created", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    entry_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Signed. topup/refund/release > 0; usage/reserve < 0; adjustment either.
    amount_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    balance_after_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reference: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class PaymentMethod(Base, TenantScoped, TimestampMixin):
    __tablename__ = "payment_methods"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "stripe_payment_method_id", name="uq_payment_methods_org_pm"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    stripe_customer_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    stripe_payment_method_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brand: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
    last4: Mapped[str] = mapped_column(sa.String(4), nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
