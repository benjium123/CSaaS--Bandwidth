"""H1 operator audit log (models/security.py OperatorAuditEntry).

The operator guards (auth/deps.py) call ``tag`` once they have accepted a request; the
``request_context`` middleware in main.py calls ``write`` after the response for every tagged
request that is not a read. One place, so a new ops route is audited without remembering to.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

import structlog
from fastapi import Request

from app.models import OperatorAuditEntry

log = structlog.get_logger(__name__)

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REASON_HEADER = "X-Ops-Reason"
_STATE_KEY = "ops_audit"


def tag(
    request: Request,
    *,
    user_id: uuid.UUID | None,
    email: str | None,
    role: str,
    log_reads: bool = False,
    org_id: uuid.UUID | None = None,
) -> None:
    """Mark ``request`` as made by an operator (or by the shared token: user_id None).
    ``log_reads``: record reads too (H3 view as workspace records every page opened)."""
    route = request.scope.get("route")
    setattr(
        request.state,
        _STATE_KEY,
        {
            "user_id": user_id,
            "email": email,
            "role": role,
            "route": getattr(route, "path", None) or request.url.path,
            "path_params": {k: str(v) for k, v in request.path_params.items()},
            "log_reads": log_reads,
            "org_id": org_id,
        },
    )


def reason_of(request: Request) -> str | None:
    """The operator's reason. The console URL-encodes it (header values are Latin-1)."""
    raw = (request.headers.get(REASON_HEADER) or "").strip()
    return unquote(raw).strip()[:500] or None


def tagged(request: Request) -> dict | None:
    return getattr(request.state, _STATE_KEY, None)


def _org_id(params: dict) -> uuid.UUID | None:
    raw = params.get("org_id")
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


async def write(request: Request, status_code: int) -> None:
    """Persist one row for a tagged, non-read request. Never raises: a failed audit write
    is logged loudly but must not turn a completed action into a 500."""
    info = tagged(request)
    if info is None or (request.method in READ_METHODS and not info.get("log_reads")):
        return
    from app.db.session import get_sessionmaker
    from app.net import client_ip

    reason = reason_of(request)
    row = OperatorAuditEntry(
        id=uuid.uuid4(),
        at=datetime.now(timezone.utc),
        operator_user_id=info["user_id"],
        operator_email=info["email"],
        operator_role=info["role"],
        method=request.method,
        route=info["route"][:255],
        path_params=info["path_params"],
        org_id=info.get("org_id") or _org_id(info["path_params"]),
        status_code=status_code,
        reason=reason,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )
    try:
        async with get_sessionmaker()() as session:
            session.add(row)
            await session.commit()
    except Exception as exc:  # pragma: no cover - exercised only on a DB outage
        log.error(
            "operator_audit_write_failed",
            route=row.route,
            method=row.method,
            operator_user_id=str(row.operator_user_id),
            error=type(exc).__name__,
        )
