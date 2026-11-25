"""Application factory.

Implemented so far: configuration checks, structured logging, liveness and
readiness, the management API's authentication and error envelope, and
credential storage. Later slices add the product features.

Run locally:
    alembic upgrade head
    uvicorn revenant.main:app --port 8000 --workers 1 --no-access-log
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from revenant import __version__
from revenant.api import (
    adapters,
    credentials,
    evidence,
    projects,
    reconstruction,
    runtime,
)
from revenant.api.errors import install_error_handlers
from revenant.config import Settings, startup_problems
from revenant.db.session import Database
from revenant.errors import ConfigurationError, NotConfigured
from revenant.logs import RequestLogMiddleware, configure_logging
from revenant.runtime.dispatcher import HostDispatcher
from revenant.runtime.lease import Lease, Timing
from revenant.runtime.proxy import RuntimeProxy
from revenant.runtime.service import recover
from revenant.runtime.state import RuntimeState
from revenant.runtime.traces import TracePipeline
from revenant.security.credentials import MasterKeys
from revenant.security.outbound import GuardedBackend, OutboundPolicy, build_outbound_client
from revenant.security.redaction import SECRETS


def create_app(settings: Settings | None = None, *, supervised: bool | None = None) -> FastAPI:
    """Build the application. Refuses to build with unsafe configuration.

    ``supervised`` overrides detection of a multi-process supervisor; tests use it.
    """
    settings = settings or Settings()
    configure_logging(settings.log_level)
    log = structlog.get_logger("revenant.startup")

    # Known secrets are scrubbed from every log line, whatever code logs them.
    for secret in (settings.secret_key, settings.secret_key_previous, settings.admin_token):
        SECRETS.register(secret.get_secret_value())

    problems = startup_problems(settings, supervised=supervised)
    if problems:
        # Problem texts name settings, never their values.
        log.error("startup_refused", env=settings.env, problems=problems)
        raise ConfigurationError(problems)

    database = Database(settings.database_url)
    state = RuntimeState()
    timing = Timing.from_settings(settings)
    traces = TracePipeline(database, settings.trace_queue_size, settings.runtime_event_retention)
    backend = GuardedBackend(policy=OutboundPolicy.from_settings(settings))
    client = build_outbound_client(backend, timeout=settings.upstream_timeout_seconds)
    lease = Lease(database, timing)
    proxy = RuntimeProxy(state, lease, traces, client, settings)
    # Ownership lost: every request in progress is cancelled at once.
    lease.on_lost = lambda: proxy.cancel_all("lease_lost")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()

        async def hand_over() -> None:
            # Stop accepting, let requests finish, drain traces, and only then release.
            await proxy.wait_idle(timing.lifetime)
            await traces.drain(timing.trace_drain)
            await asyncio.to_thread(lease.release)

        def begin_hand_over() -> None:
            # Called from the renewal thread; the drain itself runs on the event loop.
            loop.call_soon_threadsafe(lambda: loop.create_task(hand_over()))

        lease.on_handover = begin_hand_over
        try:
            state.keys = MasterKeys.from_settings(settings)
        except NotConfigured:
            state.keys = None
        if await asyncio.to_thread(database.schema_is_current):
            # No runtime traffic is served before this succeeds.
            await asyncio.to_thread(lease.acquire_or_wait, force=settings.lease_force_acquire)
            lease.start()
            await asyncio.to_thread(recover, database, settings, state)
            traces.start()
        else:
            log.warning("runtime_not_started", reason="the database schema is not current")
        try:
            yield
        finally:
            lease.stop_accepting()
            await proxy.wait_idle(timing.lifetime)
            await traces.drain(timing.trace_drain)
            await asyncio.to_thread(lease.stop)
            await asyncio.to_thread(lease.release)
            await client.aclose()
            database.dispose()

    app = FastAPI(
        title="API Revenant",
        version=__version__,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.runtime = state
    app.state.lease = lease
    app.state.traces = traces
    app.state.proxy = proxy
    app.state.outbound_backend = backend
    # Path entry. The proxy reads the original path itself, so nothing here rewrites it.
    app.mount("/runtime", cast(Any, proxy))
    # What the server runs: the dispatcher decides by host before anything else.
    app.state.dispatcher = HostDispatcher(app, proxy, state, settings)

    install_error_handlers(app)
    if settings.cors_origin_list:
        # Explicit origins only; '*' is rejected at start-up. The admin token is
        # sent in the Authorization header, so cookies are not allowed.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Request-Id"],
        )
    app.add_middleware(RequestLogMiddleware)
    app.include_router(projects.router)
    app.include_router(evidence.router)
    app.include_router(reconstruction.router)
    app.include_router(adapters.router)
    app.include_router(credentials.router)
    app.include_router(runtime.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is running. No dependencies are checked."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness: configuration valid, database reachable and migrated."""
        reachable = database.ping()
        current = reachable and database.schema_is_current()
        serving = lease.admit() is None
        checks = {
            "configuration": "ok",
            "database": "ok" if reachable else "unavailable",
            "migrations": "ok" if current else ("pending" if reachable else "unknown"),
            "lease": "held" if serving else lease.describe()["state"],
            "recovery": "done" if state.recovered else "pending",
        }
        ready = reachable and current and serving and state.recovered
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    if not settings.admin_token.get_secret_value():
        log.warning("admin_token_not_configured", effect="management API rejects every request")
    log.info("startup_complete", env=settings.env, version=__version__)
    return app


def __getattr__(name: str) -> HostDispatcher:
    # `uvicorn revenant.main:app` builds the application on first access, so
    # importing this module (for example in tests) has no side effects. What the
    # server runs is the host dispatcher, which wraps the management application.
    if name == "app":
        dispatcher: HostDispatcher = create_app().state.dispatcher
        return dispatcher
    raise AttributeError(name)
