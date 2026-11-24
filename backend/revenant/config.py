"""Process configuration, read from the environment.

Only settings that existing code uses are defined here. More are added as
later slices need them.
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that appear in .env.example or the documentation. They are never
# acceptable in production.
_PLACEHOLDER_PREFIXES = ("CHANGE_ME",)
_MIN_ADMIN_TOKEN_LENGTH = 24


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVENANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "demo", "production"] = "development"
    log_level: str = "INFO"

    # Master key (Fernet format) and management API token. Required in production.
    secret_key: SecretStr = SecretStr("")
    admin_token: SecretStr = SecretStr("")

    # Outbound safety. See docs/security.md.
    allow_private_upstreams: bool = False
    upstream_allowlist: str = ""
    upstream_allowed_ports: str = "80,443,8000,8080,8443"
    upstream_timeout_seconds: float = 30.0

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def allowlisted_hosts(self) -> frozenset[str]:
        return frozenset(
            item.strip().lower().rstrip(".")
            for item in self.upstream_allowlist.split(",")
            if item.strip()
        )

    @property
    def allowed_ports(self) -> frozenset[int]:
        ports: set[int] = set()
        for item in self.upstream_allowed_ports.split(","):
            text = item.strip()
            if text:
                ports.add(int(text))
        return frozenset(ports)


def _is_placeholder(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def _is_fernet_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value.encode("ascii"))) == 32
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False


def startup_problems(settings: Settings) -> list[str]:
    """Return every reason the process must not start with these settings."""
    problems: list[str] = []
    secret_key = settings.secret_key.get_secret_value()
    admin_token = settings.admin_token.get_secret_value()

    try:
        ports = settings.allowed_ports
    except ValueError:
        problems.append("REVENANT_UPSTREAM_ALLOWED_PORTS must be a comma-separated list of numbers")
    else:
        if any(port < 1 or port > 65535 for port in ports):
            problems.append("REVENANT_UPSTREAM_ALLOWED_PORTS contains a port outside 1-65535")

    if secret_key and not _is_placeholder(secret_key) and not _is_fernet_key(secret_key):
        problems.append("REVENANT_SECRET_KEY is not a valid Fernet key")

    if settings.is_production:
        if not secret_key or _is_placeholder(secret_key):
            problems.append("REVENANT_SECRET_KEY must be set to a generated key in production")
        if not admin_token or _is_placeholder(admin_token):
            problems.append("REVENANT_ADMIN_TOKEN must be set to a generated token in production")
        elif len(admin_token) < _MIN_ADMIN_TOKEN_LENGTH:
            problems.append(
                f"REVENANT_ADMIN_TOKEN must be at least {_MIN_ADMIN_TOKEN_LENGTH} characters"
            )
        if settings.allow_private_upstreams:
            problems.append("REVENANT_ALLOW_PRIVATE_UPSTREAMS must not be enabled in production")
    return problems
