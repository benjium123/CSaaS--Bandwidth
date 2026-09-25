"""P44d: refuse throwaway (disposable) email addresses at signup and invite.

Burner inboxes are how one person opens many workspaces: a fresh address per signup, no
trace back to a real identity. The list is the community-maintained, CC0
disposable-email-domains blocklist (app/data/disposable_domains.txt, ~9,000 domains);
refresh it with backend/scripts/refresh_disposable_domains.py. Operators can add more
domains with DISPOSABLE_EXTRA_DOMAINS (comma separated). Subdomains match too:
"x.mailinator.com" is refused because "mailinator.com" is listed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.errors import ValidationFailedError

_LIST = Path(__file__).resolve().parent.parent / "data" / "disposable_domains.txt"

REFUSAL = "Please use a permanent work or personal email address, not a temporary one."


@lru_cache(maxsize=1)
def _domains() -> frozenset[str]:
    try:
        lines = _LIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        line.strip().lower() for line in lines if line.strip() and not line.startswith("#")
    )


def _extra() -> frozenset[str]:
    from app.config import get_active_settings

    try:
        raw = getattr(get_active_settings(), "disposable_extra_domains", "") or ""
    except Exception:  # noqa: BLE001 - config trouble must not break signup
        raw = ""
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def is_disposable(email: str) -> bool:
    domain = (email or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if not domain or "@" not in (email or ""):
        return False
    blocked = _domains() | _extra()
    parts = domain.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))


def require_permanent(email: str) -> None:
    if is_disposable(email):
        raise ValidationFailedError(REFUSAL)
