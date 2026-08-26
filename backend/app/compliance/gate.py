"""The compliance seam.

P3 owns compliance. P1 owns the SEAM — a single choke point every outbound message passes
through, and a single hook every inbound message passes through, so P3 fills them in rather
than retrofitting checks into N call sites later.

**There is deliberately no compliance logic here.** No STOP parsing, no quiet hours, no DNC.
P1's verdict is always allow. Tests pin the seam with a spy so it cannot be bypassed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class OutboundDraft:
    to_e164: str
    from_e164: str
    body: str


@dataclass(frozen=True)
class ComplianceVerdict:
    allowed: bool
    reason: str | None = None


async def check_outbound(
    session: AsyncSession, org_id: uuid.UUID, draft: OutboundDraft
) -> ComplianceVerdict:
    """Called EXACTLY ONCE per send, before any row is created and before the carrier is
    touched. A deny must cost nothing and leave no trace in `messages`.

    P3 will implement here: opt-out suppression across the whole number pool, quiet hours
    in the RECIPIENT's timezone, DNC, and consent checks.
    """
    return ComplianceVerdict(allowed=True)


async def on_inbound(session: AsyncSession, org_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Called once per ingested inbound message, after it is persisted.

    P3 will hang STOP / HELP / START keyword handling off this hook.
    """
    return None
