from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth as auth_routes
from app.api.routes import calls as call_routes
from app.api.routes import compliance as compliance_routes
from app.api.routes import contacts as contact_routes
from app.api.routes import health as health_routes
from app.api.routes import inbox as inbox_routes
from app.api.routes import media as media_routes
from app.api.routes import messages as message_routes
from app.api.routes import numbers as number_routes
from app.api.routes import orgs as org_routes
from app.api.routes import registration as registration_routes
from app.api.routes import routing as routing_routes
from app.api.routes import softphone as softphone_routes
from app.api.routes import templates as template_routes
from app.api.routes import twofa as twofa_routes
from app.api.routes import webhooks as webhook_routes
from app.config import Settings, load_settings
from app.db.session import dispose_engine, init_engine
from app.errors import CsaasError
from app.events.bus import EventBus
from app.logging import configure_logging
from app.providers.registry import build_registry
from app.storage.base import build_store
from app.voice_plane import service as voice_service

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
        app.state.carriers = build_registry(settings)
        # Kept as the PRIMARY, not as "the" carrier: the P1/P2 seam tests read it,
        # and they are the evidence the abstraction held.
        app.state.carrier = app.state.carriers.primary()
        app.state.media_store = build_store(
            settings.media_store_backend, root=settings.media_local_root
        )
        # P6: media plane, not a carrier - app.state.livekit is None when unconfigured
        # (settings.livekit_url / livekit_api_secret unset), and every route/webhook that
        # needs it 503s or 404s on that None rather than crashing boot.
        app.state.event_bus = EventBus()
        # (finding 15d) built ONCE, eagerly below - never rebuilt here, so there is only
        # ever one LiveKitApi (and one owned httpx client) to close, right next to the
        # carrier registry's own aclose().
        structlog.get_logger("startup").info(
            "carrier_configured",
            carrier=getattr(app.state.carrier, "name", None),
            carriers=app.state.carriers.names(),
            media_store=app.state.media_store.name,
        )

        sweeper_task = None
        if settings.sweeper_enabled:
            from app.services.sweeper import sweeper_loop

            sweeper_task = asyncio.create_task(
                sweeper_loop(app, settings.sweeper_interval_seconds)
            )
        try:
            yield
        finally:
            if sweeper_task is not None:
                sweeper_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper_task
            registry = getattr(app.state, "carriers", None)
            if registry is not None:
                await registry.aclose()
            livekit_api = getattr(app.state, "livekit", None)
            if livekit_api is not None:
                await livekit_api.aclose()
            await dispose_engine()

    app = FastAPI(
        title="CSaaS API",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Set eagerly too: tests drive the app without running lifespan, and override it.
    app.state.carriers = build_registry(settings)
    app.state.carrier = app.state.carriers.primary()
    app.state.media_store = build_store(
        settings.media_store_backend, root=settings.media_local_root
    )
    app.state.event_bus = EventBus()
    app.state.livekit = voice_service.make_api(settings)

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
    app.include_router(twofa_routes.router)
    app.include_router(number_routes.router)
    app.include_router(contact_routes.router)
    app.include_router(inbox_routes.router)
    app.include_router(compliance_routes.router)
    app.include_router(routing_routes.router)
    app.include_router(registration_routes.router)
    app.include_router(media_routes.router)
    app.include_router(template_routes.router)
    app.include_router(message_routes.router)
    app.include_router(call_routes.router)
    app.include_router(softphone_routes.router)
    app.include_router(webhook_routes.router)
    return app


app = create_app  # uvicorn factory target: `uvicorn app.main:app --factory`
