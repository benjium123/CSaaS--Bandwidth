"""Support chat AI agent: which chats the assistant is still answering.

``site_chats.ai_state``: NULL = the assistant is not on this chat (every chat before this
migration, and a chat a person was asked for during staffed hours); ``active`` = the
assistant answers each new visitor message; ``assist`` = it answers while the team is also
alerted (a person was asked for out of hours); ``handoff`` = the assistant passed the chat to
the team; ``off`` = an operator replied or took the chat. Additive and nullable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096_support_chat_ai"
down_revision = "0095_support_chat_inbox"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_chats", sa.Column("ai_state", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("site_chats", "ai_state")
