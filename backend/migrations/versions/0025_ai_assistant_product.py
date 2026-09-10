"""P23: AI assistant as a product - per-org AI provider keys, builder fields, outcomes.

- ai_provider_accounts             per-org encrypted LLM / STT / TTS credentials (P17 pattern)
- orgs.ai_key_mode                 'platform' (billed per use) | 'byok' (org's own keys)
- agent_profiles.*                 goals, guardrails, language, stt/tts provider, behaviour
                                   limits, tools JSON, post_call_fields JSON
- kb_documents.status/storage_key/chunk_count   ingestion state for uploads (KB search exists)
- call_scores.*                    outcome fields (Fable decision: extend call_scores instead
                                   of a new call_outcomes table - it already holds summary,
                                   sentiment, score, tokens per call)
- outbound_campaigns.agent_profile_id           AI outbound campaigns

Additive. Filename keeps the roadmap number (0025) but the chain continues from the true
head 0039_smart_routing.

Revision ID: 0025_ai_assistant_product
Revises: 0039_smart_routing
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0025_ai_assistant_product"
down_revision = "0039_smart_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_accounts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),  # llm | stt | tts
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("label", sa.String(127), nullable=False, server_default=""),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_detail", sa.String(512), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "kind", "provider", "label", name="uq_ai_provider_accounts_org_kind_provider"
        ),
    )
    op.create_index("ix_ai_provider_accounts_org_id", "ai_provider_accounts", ["org_id"])

    op.add_column(
        "orgs",
        sa.Column("ai_key_mode", sa.String(8), nullable=False, server_default="platform"),
    )

    for name, col in (
        ("goals", sa.Column("goals", sa.Text(), nullable=False, server_default="")),
        ("guardrails", sa.Column("guardrails", sa.Text(), nullable=False, server_default="")),
        ("language", sa.Column("language", sa.String(8), nullable=False, server_default="en")),
        ("stt_provider", sa.Column("stt_provider", sa.String(16), nullable=False, server_default="")),
        ("tts_provider", sa.Column("tts_provider", sa.String(16), nullable=False, server_default="")),
        (
            "max_call_seconds",
            sa.Column("max_call_seconds", sa.Integer(), nullable=False, server_default="900"),
        ),
        (
            "silence_timeout_seconds",
            sa.Column("silence_timeout_seconds", sa.Integer(), nullable=False, server_default="12"),
        ),
        (
            "interrupt_sensitivity",
            sa.Column(
                "interrupt_sensitivity", sa.String(8), nullable=False, server_default="medium"
            ),
        ),
        (
            "voicemail_action",
            sa.Column(
                "voicemail_action", sa.String(16), nullable=False, server_default="leave_message"
            ),
        ),
        ("tools", sa.Column("tools", PortableJSON(), nullable=False, server_default="[]")),
        (
            "post_call_fields",
            sa.Column("post_call_fields", PortableJSON(), nullable=False, server_default="[]"),
        ),
    ):
        op.add_column("agent_profiles", col)

    op.add_column(
        "kb_documents",
        sa.Column("status", sa.String(16), nullable=False, server_default="indexed"),
    )
    op.add_column("kb_documents", sa.Column("storage_key", sa.String(255), nullable=True))
    op.add_column(
        "kb_documents",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.add_column("call_scores", sa.Column("disposition", sa.String(32), nullable=True))
    op.add_column("call_scores", sa.Column("intent", sa.String(64), nullable=True))
    op.add_column(
        "call_scores",
        sa.Column("extracted", PortableJSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "call_scores", sa.Column("handoff", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "call_scores", sa.Column("booked", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "call_scores",
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "call_scores",
        sa.Column(
            "profile_id",
            GUID(),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "call_scores",
        sa.Column(
            "follow_up_sms_message_id",
            GUID(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.add_column(
        "outbound_campaigns",
        sa.Column(
            "agent_profile_id",
            GUID(),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("outbound_campaigns", "agent_profile_id")
    for name in (
        "follow_up_sms_message_id",
        "profile_id",
        "is_test",
        "booked",
        "handoff",
        "extracted",
        "intent",
        "disposition",
    ):
        op.drop_column("call_scores", name)
    for name in ("chunk_count", "storage_key", "status"):
        op.drop_column("kb_documents", name)
    for name in (
        "post_call_fields",
        "tools",
        "voicemail_action",
        "interrupt_sensitivity",
        "silence_timeout_seconds",
        "max_call_seconds",
        "tts_provider",
        "stt_provider",
        "language",
        "guardrails",
        "goals",
    ):
        op.drop_column("agent_profiles", name)
    op.drop_column("orgs", "ai_key_mode")
    op.drop_index("ix_ai_provider_accounts_org_id", table_name="ai_provider_accounts")
    op.drop_table("ai_provider_accounts")
