"""Plivo carrier package (phase-9b DR-3: the proof the CAL abstraction is real).

Plivo is the first carrier that is not Bandwidth-, Telnyx- or Twilio-shaped: Basic
auth-id/auth-token credentials, a V3 signature scheme (HMAC-SHA256 over url+nonce, not
body), its own XML dialect, and a prose-only (no code table) error taxonomy. See
docs/plans/phase-9b-provider-parity.md.
"""

from __future__ import annotations
