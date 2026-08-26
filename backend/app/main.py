from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth as auth_routes
from app.api.routes import health as health_routes
from app.api.routes import messages as message_routes
from app.api.routes import numbers as number_routes
from app.api.routes import orgs as org_routes
from app.api.routes import webhooks as webhook_routes
from app.config import Settings, load_settings
from app.db.session import dispose_engine, init_engine
from app.errors import CsaasError
from app.logging import configure_logging
from app.providers.base import build_carrier

VERSION = "0.1.0"


def _log_provider_report(settings: Settings) -> None:
    """One line per integration. Names missing VARIABLES, never their values."""
    log = structlog.get_logger("startup")
    for status in settings.provider_statuses():
        log.info(
            "provider_status",
            provider=status.name,
            enabled=status.enabled,
            reason=status.reason,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(env=settings.app_env, level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log_provider_report(settings)
        init_engine(settings.database_url)
        # None when Bandwidth is not configured — the app must still boot and serve
        # /healthz. Sending then answers 503 carrier_not_configured.
        app.state.carrier = build_carrier(settings)
        structlog.get_logger("startup").info(
            "carrier_configured", carrier=getattr(app.state.carrier, "name", None)
        )
        try:
            yield
        finally:
            carrier = getattr(app.state, "carrier", None)
            if carrier is not None and hasattr(carrier, "aclose"):
                await carrier.aclose()
            await dispose_engine()

    app = FastAPI(
        title="CSaaS API",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Set eagerly too: tests drive the app without running lifespan, and override it.
    app.state.carrier = build_carrier(settings)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            structlog.get_logger("http").exception(
                "request_failed", duration_ms=round((time.perf_counter() - started) * 1000, 2)
            )
            raise
        response.headers["X-Request-Id"] = request_id
        structlog.get_logger("http").info(
            "request",
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response

    @app.exception_handler(CsaasError)
    async def handle_csaas_error(request: Request, exc: CsaasError) -> JSONResponse:
        request_id = request.headers.get("X-Request-Id", "")
        if exc.http_status >= 500:
            # missing_tenant_context lands here. It means a programming bug, not bad input.
            structlog.get_logger("error").error(
                "server_error", code=exc.code, message=exc.message
            )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {"code": exc.code, "message": exc.message, "request_id": request_id}
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = request.headers.get("X-Request-Id", "")
        structlog.get_logger("error").exception("unhandled_exception")
        # Never leak internals into the response body.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal error",
                    "request_id": request_id,
                }
            },
        )

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(org_routes.router)
    app.include_router(number_routes.router)
    app.include_router(message_routes.router)
    app.include_router(webhook_routes.router)
    return app


app = create_app  # uvicorn factory target: `uvicorn app.main:app --factory`
