"""Domain errors shared across the backend."""

from __future__ import annotations

from typing import Any


class ConfigurationError(Exception):
    """The process configuration is unsafe or incomplete. Raised at start-up."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class OutboundBlocked(Exception):
    """An outbound destination failed validation. No connection was attempted."""


class ApiError(Exception):
    """An error with a stable code, rendered in the common error envelope.

    ``message`` and ``details`` are written for the caller. They must never
    contain a credential value.
    """

    status_code = 400
    code = "validation_failed"

    def __init__(
        self,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ValidationFailed(ApiError):
    status_code = 400
    code = "validation_failed"


class Unauthenticated(ApiError):
    status_code = 401
    code = "unauthenticated"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class NotConfigured(ApiError):
    """A feature needs configuration the operator has not supplied."""

    status_code = 409
    code = "not_configured"


class DatabaseUnavailable(ApiError):
    status_code = 503
    code = "database_unavailable"
