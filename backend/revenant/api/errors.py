"""The common error envelope (docs/api-contracts.md).

    { "error": { "code": "...", "message": "...", "details": [ ... ] } }

Unexpected errors are reported without their message, so internal details and
credential values cannot leak through an error response.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from revenant.errors import ApiError
from revenant.logs import error_fields

log = structlog.get_logger("revenant.api")

_STATUS_CODES = {
    400: "validation_failed",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
}


def envelope(
    status_code: int, code: str, message: str, details: list[dict[str, Any]] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or []}}
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return JSONResponse(body, status_code=status_code, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only. The submitted values are left out,
        # because a rejected body may contain a credential.
        details = [
            {"field": ".".join(str(part) for part in error["loc"] if part != "body"),
             "issue": error["msg"]}
            for error in exc.errors()
        ]
        return envelope(400, "validation_failed", "The request is not valid.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return envelope(exc.status_code, code, message)

    @app.exception_handler(OperationalError)
    async def _database_down(_request: Request, exc: OperationalError) -> JSONResponse:
        log.error("database_unavailable", **error_fields(exc))
        return envelope(503, "database_unavailable", "The database is not available.")

    @app.exception_handler(DBAPIError)
    async def _database_error(_request: Request, exc: DBAPIError) -> JSONResponse:
        log.error("database_error", **error_fields(exc))
        return envelope(500, "internal_error", "An internal error occurred.")

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", **error_fields(exc))
        return envelope(500, "internal_error", "An internal error occurred.")
