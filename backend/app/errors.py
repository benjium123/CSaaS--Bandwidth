"""Error taxonomy.

Every error the API returns has a stable machine-readable ``code``. Response bodies never
carry tracebacks or internal detail; that goes to the logs only.
"""

from __future__ import annotations


class CsaasError(Exception):
    """Base for every error we deliberately surface."""

    code: str = "internal_error"
    http_status: int = 500
    message: str = "Internal error"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        super().__init__(self.message)


class NotFoundError(CsaasError):
    code = "not_found"
    http_status = 404
    message = "Not found"


class PermissionDeniedError(CsaasError):
    code = "permission_denied"
    http_status = 403
    message = "Permission denied"


class UnauthenticatedError(CsaasError):
    code = "unauthenticated"
    http_status = 401
    message = "Not authenticated"


class StepUpRequiredError(CsaasError):
    """P41: the action needs fresh proof of identity. ``kind`` is recent_2fa (authenticator
    app / passkey) or recent_selfie (Stripe Identity); ``action`` names what is being
    unlocked so the console can resume it afterwards."""

    code = "step_up_required"
    http_status = 403
    message = "Please confirm it is you to continue"

    def __init__(self, message: str | None = None, *, kind: str, action: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.action = action


class AccountNotVerifiedError(CsaasError):
    """P41: calling, texting and numbers wait for business verification."""

    code = "account_not_verified"
    http_status = 403
    message = "Calling and texting unlock once your business verification is approved"


class AccountSuspendedError(CsaasError):
    code = "account_suspended"
    http_status = 403
    message = "This account is suspended. Contact support."


class ValidationFailedError(CsaasError):
    code = "validation_failed"
    http_status = 422
    message = "Validation failed"


class ConflictError(CsaasError):
    code = "conflict"
    http_status = 409
    message = "Conflict"


class RateLimitExceededError(CsaasError):
    """C7: raised by app/rate_limit.py so a 429 uses the SAME ``{"error": {...}}``
    envelope every other error already does, instead of FastAPI's bare
    ``{"detail": ...}`` HTTPException shape. ``retry_after`` (seconds) is read by
    main.py's CsaasError handler and re-attached as the ``Retry-After`` header."""

    code = "rate_limited"
    http_status = 429
    message = "Too many requests"

    def __init__(self, message: str | None = None, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MissingTenantContextError(CsaasError):
    """A tenant-scoped query ran with no org context.

    This is a PROGRAMMING BUG, never a user error: it means code reached the database
    without establishing which org it was acting for. Surfacing it as a 500 and logging at
    ERROR is deliberate — the alternative (silently returning every org's rows) is a data
    breach.
    """

    code = "missing_tenant_context"
    http_status = 500
    message = "Tenant context was not established for a tenant-scoped query"


class ConfigurationError(CsaasError):
    """Settings failed validation at boot."""

    code = "configuration_error"
    http_status = 500
    message = "Invalid configuration"


class CarrierNotConfiguredError(CsaasError):
    """No messaging carrier is configured. This is the R1 reality until Bandwidth
    credentials exist: the app boots and serves /healthz, but sending is a 503."""

    code = "carrier_not_configured"
    http_status = 503
    message = "No messaging carrier is configured"


class ComplianceBlockedError(CsaasError):
    code = "compliance_blocked"
    http_status = 422
    message = "Blocked by compliance policy"


class CarrierCapabilityError(CsaasError):
    """The selected carrier cannot express the requested operation.

    Callers should consult ``carrier.capabilities`` first rather than discovering this.
    """

    code = "carrier_capability_unsupported"
    http_status = 501
    message = "The configured carrier does not support this operation"


class InsufficientCreditsError(CsaasError):
    """P24: the workspace's prepaid credits cannot cover the requested AI usage."""

    code = "insufficient_credits"
    http_status = 402
    message = "Add credits to keep your assistant answering"


class FeatureUnavailableError(CsaasError):
    """A feature is correctly implemented but its prerequisite config is absent."""

    code = "feature_unavailable"
    http_status = 503
    message = "This feature is not available with the current configuration"


class StickySenderUnavailableError(CsaasError):
    """The number this conversation has always used is no longer active.

    Deliberately loud. Silently sending from a different number is the classic
    number-pool bug: the recipient sees a stranger, and STOP handling gets confusing.
    The caller must opt in to reassignment explicitly.
    """

    code = "sticky_sender_unavailable"
    http_status = 422
    message = "The number this conversation uses is no longer active"
