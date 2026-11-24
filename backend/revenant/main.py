"""Application factory.

Slice 1 scope: configuration checks, structured logging, liveness and
readiness. No business features yet.

Run locally:
    uvicorn revenant.main:app --port 8000 --workers 1 --no-access-log
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from revenant import __version__
from revenant.config import Settings, startup_problems
from revenant.errors import ConfigurationError
from revenant.logs import RequestLogMiddleware, configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Refuses to build with unsafe configuration."""
    settings = settings or Settings()
    configure_logging(settings.log_level)
    log = structlog.get_logger("revenant.startup")

    problems = startup_problems(settings)
    if problems:
        # Problem texts name settings, never their values.
        log.error("startup_refused", env=settings.env, problems=problems)
        raise ConfigurationError(problems)

    app = FastAPI(
        title="API Revenant",
        version=__version__,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.add_middleware(RequestLogMiddleware)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is running. No dependencies are checked."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, object]:
        """Readiness. Later slices add database, lease and recovery checks."""
        return {"status": "ready", "checks": {"configuration": "ok"}}

    log.info("startup_complete", env=settings.env, version=__version__)
    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn revenant.main:app` builds the application on first access, so
    # importing this module (for example in tests) has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
