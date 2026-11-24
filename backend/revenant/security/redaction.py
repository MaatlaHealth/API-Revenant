"""Redaction for logs and trace snapshots (docs/security.md, "Logging and traces").

This is the second layer. The first is structural: credentials are removed
from a request before it is snapshotted. The redactor then:

* replaces the values of credential-carrying headers by name;
* replaces values at configured sensitive paths;
* replaces any occurrence of a known secret value, wherever it appears.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any
from urllib.parse import quote

REDACTED = "[REDACTED]"

SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
# Log event keys whose values are never emitted, whatever they contain.
SENSITIVE_KEYS = frozenset(
    {"password", "secret", "secret_key", "admin_token", "token", "api_key", "credential",
     "authorization", "ciphertext", "verifier", "value"}
)
_MIN_SECRET_LENGTH = 6


class SecretRegistry:
    """Secret values known to this process, so they can be scrubbed from any output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: frozenset[str] = frozenset()

    def register(self, value: str) -> None:
        # Very short values would turn ordinary text into "[REDACTED]".
        if len(value) < _MIN_SECRET_LENGTH:
            return
        with self._lock:
            self._values = self._values | {value, quote(value, safe="")}

    def forget(self, value: str) -> None:
        with self._lock:
            self._values = self._values - {value, quote(value, safe="")}

    def clear(self) -> None:
        with self._lock:
            self._values = frozenset()

    def values(self) -> frozenset[str]:
        return self._values


SECRETS = SecretRegistry()


class Redactor:
    def __init__(
        self,
        *,
        extra_headers: Iterable[str] = (),
        sensitive_paths: Iterable[str] = (),
        secret_values: Iterable[str] = (),
        registry: SecretRegistry | None = SECRETS,
    ) -> None:
        self.headers = SENSITIVE_HEADERS | {name.lower() for name in extra_headers}
        self.paths = {_parse_path(path) for path in sensitive_paths}
        self._own_values = frozenset(
            item
            for value in secret_values
            if len(value) >= _MIN_SECRET_LENGTH
            for item in (value, quote(value, safe=""))
        )
        self.registry = registry

    def _secrets(self) -> frozenset[str]:
        return self._own_values | (self.registry.values() if self.registry else frozenset())

    def text(self, value: str) -> str:
        # Longest first, so a secret that contains another is replaced whole.
        for secret in sorted(self._secrets(), key=len, reverse=True):
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value

    def headers_map(self, headers: Mapping[str, str]) -> dict[str, str]:
        return {
            name: REDACTED if name.lower() in self.headers else self.text(value)
            for name, value in headers.items()
        }

    def value(self, obj: Any, _path: tuple[str | int, ...] = ()) -> Any:
        """Return a redacted deep copy of a JSON-like value."""
        if _path in self.paths:
            return REDACTED
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, bytes):
            return self.text(obj.decode("utf-8", errors="replace"))
        if isinstance(obj, Mapping):
            return {
                (self.text(key) if isinstance(key, str) else key): self.value(
                    item, (*_path, key if isinstance(key, (str, int)) else str(key))
                )
                for key, item in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self.value(item, (*_path, index)) for index, item in enumerate(obj)]
        return obj


def _parse_path(path: str) -> tuple[str | int, ...]:
    """Parse ``$.a.b[0]`` into ``("a", "b", 0)``."""
    parts: list[str | int] = []
    token = ""
    text = path[1:] if path.startswith("$") else path
    index = 0
    while index < len(text):
        char = text[index]
        if char == ".":
            if token:
                parts.append(token)
                token = ""
        elif char == "[":
            if token:
                parts.append(token)
                token = ""
            end = text.index("]", index)
            parts.append(int(text[index + 1:end]))
            index = end
        else:
            token += char
        index += 1
    if token:
        parts.append(token)
    return tuple(parts)


_LOG_REDACTOR = Redactor()


def redact_log_event(
    _logger: Any, _method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: scrub every log event before it is rendered."""
    for key in list(event):
        if key.lower() in SENSITIVE_KEYS or key.lower() in SENSITIVE_HEADERS:
            event[key] = REDACTED
        elif key == "headers" and isinstance(event[key], Mapping):
            event[key] = _LOG_REDACTOR.headers_map(
                {str(name): str(value) for name, value in event[key].items()}
            )
        else:
            event[key] = _LOG_REDACTOR.value(event[key])
    return event
