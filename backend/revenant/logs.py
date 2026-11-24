"""Structured logging and the application's own request log.

Two rules from docs/security.md are enforced here:

* A request is logged with its path only. The query string is never logged,
  because it may carry a credential. (The HTTP server's built-in access log,
  which prints full request targets, is disabled with ``--no-access-log``.)
* Exception messages are never logged raw. Errors are logged by type.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_ID_HEADER = b"x-request-id"


def configure_logging(level: str = "INFO") -> None:
    """Emit one JSON object per log event on standard output."""
    logging.basicConfig(format="%(message)s", level=level.upper(), force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )


def error_fields(exc: BaseException) -> dict[str, str]:
    """Fields that are safe to log for an exception: its type, never its message."""
    return {"error_type": type(exc).__name__}


class RequestLogMiddleware:
    """Pure ASGI middleware that writes one log event per HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = structlog.get_logger("revenant.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        status: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        # Deliberately only scope["path"]: never scope["query_string"] or raw_path.
        fields: dict[str, Any] = {
            "method": scope.get("method"),
            "path": scope.get("path"),
            "request_id": request_id,
        }
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self.log.error(
                "http_request_failed", duration_ms=duration_ms, **fields, **error_fields(exc)
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self.log.info("http_request", status=status, duration_ms=duration_ms, **fields)
