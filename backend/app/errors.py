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


class ValidationFailedError(CsaasError):
    code = "validation_failed"
    http_status = 422
    message = "Validation failed"


class ConflictError(CsaasError):
    code = "conflict"
    http_status = 409
    message = "Conflict"


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
