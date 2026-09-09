from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock

REPLAY_TTL_SECONDS = 300

#: C4: bound memory in case an attacker (or a genuinely large deployment) sends many
#: distinct signatures. An OrderedDict so entries can be evicted oldest-by-expiry
#: (front of the dict, since insertion order tracks expiry order - see the pop-before-
#: reinsert below) when the expired sweep alone isn't enough to get back under the cap.
_MAX_ENTRIES = 10_000

_SEEN: "OrderedDict[str, float]" = OrderedDict()
_LOCK = Lock()


def check_and_record(signature: str, ttl_seconds: int = REPLAY_TTL_SECONDS) -> bool:
    """Return True if the signature is new, False if it was seen within the TTL."""
    now = time.monotonic()
    with _LOCK:
        expires_at = _SEEN.get(signature)
        if expires_at is not None and now < expires_at:
            return False

        # Pop before re-inserting (rather than plain __setitem__) so a re-added
        # signature (only reachable once its previous entry has expired) lands at the
        # END of the OrderedDict - keeping "oldest at the front" an accurate proxy for
        # "expires soonest", which the eviction below relies on.
        _SEEN.pop(signature, None)
        _SEEN[signature] = now + ttl_seconds

        # Bound memory in case an attacker sends many distinct signatures.
        if len(_SEEN) > _MAX_ENTRIES:
            expired = [k for k, v in _SEEN.items() if v <= now]
            for k in expired:
                del _SEEN[k]
            # C4: the expired sweep alone is not a bound - a burst of NOT-YET-expired
            # distinct signatures within one TTL window grows this dict without limit.
            # Evict oldest-by-expiry until back under the cap.
            while len(_SEEN) > _MAX_ENTRIES:
                _SEEN.popitem(last=False)

        return True
