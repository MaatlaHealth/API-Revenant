"""Application factory.

Implemented so far: configuration checks, structured logging, liveness and
readiness, the management API's authentication and error envelope, and
credential storage. Later slices add the product features.

Run locally:
    alembic upgrade head
    uvicorn revenant.main:app --port 8000 --workers 1 --no-access-log
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from revenant import __version__
from revenant.api import credentials
from revenant.api.errors import install_error_handlers
from revenant.config import Settings, startup_problems
from revenant.db.session import Database
from revenant.errors import ConfigurationError
from revenant.logs import RequestLogMiddleware, configure_logging
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
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
    app.include_router(credentials.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is running. No dependencies are checked."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness: configuration valid, database reachable and migrated."""
        reachable = database.ping()
        current = reachable and database.schema_is_current()
        checks = {
            "configuration": "ok",
            "database": "ok" if reachable else "unavailable",
            "migrations": "ok" if current else ("pending" if reachable else "unknown"),
        }
        ready = reachable and current
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    if not settings.admin_token.get_secret_value():
        log.warning("admin_token_not_configured", effect="management API rejects every request")
    log.info("startup_complete", env=settings.env, version=__version__)
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn revenant.main:app` builds the application on first access, so
    # importing this module (for example in tests) has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
