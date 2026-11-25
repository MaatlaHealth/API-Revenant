"""Process configuration, read from the environment.

Only settings that existing code uses are defined here. More are added as
later slices need them.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import multiprocessing
import os
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that appear in .env.example or the documentation. They are never
# acceptable in production.
_PLACEHOLDER_PREFIXES = ("CHANGE_ME",)
_MIN_ADMIN_TOKEN_LENGTH = 24
MULTI_WORKER_ERROR = "unsupported_multi_worker"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVENANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: Literal["development", "demo", "production"] = "development"
    log_level: str = "INFO"

    # Hosting platforms provide DATABASE_URL without a prefix.
    database_url: str = Field(
        default="sqlite:///./revenant.db",
        validation_alias=AliasChoices("DATABASE_URL", "REVENANT_DATABASE_URL"),
    )

    # Master key (Fernet format) and management API token. Required in production.
    secret_key: SecretStr = SecretStr("")
    # Only while rotating the master key: the old value.
    secret_key_previous: SecretStr = SecretStr("")
    admin_token: SecretStr = SecretStr("")

    # Comma-separated browser origins allowed to call the management API.
    cors_origins: str = ""

    # Exactly one worker is supported (docs/architecture.md, section 4.5).
    web_concurrency: int | None = Field(
        default=None, validation_alias=AliasChoices("WEB_CONCURRENCY")
    )
    workers: int | None = None

    # Evidence uploads. See docs/security.md, "Uploads".
    max_upload_bytes: int = 5 * 1024 * 1024
    max_evidence_files: int = 50

    # Outbound safety. See docs/security.md.
    allow_private_upstreams: bool = False
    upstream_allowlist: str = ""
    upstream_allowed_ports: str = "80,443,8000,8080,8443"
    upstream_timeout_seconds: float = 30.0

    # Runtime. See docs/architecture.md, sections 4.2, 4.5, 4.6 and 4.9.
    # Comma-separated hostnames on which the management API answers.
    public_hosts: str = ""
    trust_forwarded_host: bool = False
    trusted_proxies: str = ""
    lease_renew_seconds: float = 5.0
    lease_grace_seconds: float = 45.0
    request_lifetime_seconds: float = 30.0
    lease_clock_margin_seconds: float = 15.0
    lease_ttl_seconds: float = 90.0
    lease_wait_seconds: float = 120.0
    lease_force_acquire: bool = False
    trace_queue_size: int = 1000
    trace_drain_seconds: float = 5.0
    runtime_max_body_bytes: int = 1024 * 1024
    runtime_max_concurrency: int = 100
    runtime_event_retention: int = 5000
    # Where the bundled demonstration files are. Empty: the demo directory of the repository.
    demo_dir: str = ""

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

    @property
    def public_host_set(self) -> frozenset[str]:
        """Management hosts, lower case, without port or trailing dot."""
        return frozenset(
            item.strip().lower().rstrip(".") for item in self.public_hosts.split(",")
            if item.strip()
        )

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(item.strip(), strict=False)
                for item in self.trusted_proxies.split(",") if item.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip().rstrip("/") for item in self.cors_origins.split(",") if item.strip()]


def _is_placeholder(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def is_fernet_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value.encode("ascii"))) == 32
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False


def under_multiprocess_supervisor() -> bool:
    """True when this process was started as a worker by a process supervisor.

    Uvicorn runs a single worker in the launching process itself. It only
    spawns child processes (through ``multiprocessing``) for ``--workers N``
    with N > 1 or for ``--reload``. Gunicorn announces itself in SERVER_SOFTWARE.
    """
    if multiprocessing.parent_process() is not None:
        return True
    return "gunicorn" in os.environ.get("SERVER_SOFTWARE", "").lower()


def startup_problems(settings: Settings, *, supervised: bool | None = None) -> list[str]:
    """Return every reason the process must not start with these settings."""
    problems: list[str] = []
    secret_key = settings.secret_key.get_secret_value()
    previous_key = settings.secret_key_previous.get_secret_value()
    admin_token = settings.admin_token.get_secret_value()

    # --- exactly one worker ---------------------------------------------------
    for name, value in (("WEB_CONCURRENCY", settings.web_concurrency),
                        ("REVENANT_WORKERS", settings.workers)):
        if value is not None and value != 1:
            problems.append(
                f"{MULTI_WORKER_ERROR}: {name} is {value}; exactly one worker is supported"
            )
    if supervised is None:
        supervised = under_multiprocess_supervisor()
    # A reloader in development also runs the app in a child process; that is fine.
    if supervised and settings.env != "development":
        problems.append(
            f"{MULTI_WORKER_ERROR}: started by a multi-process supervisor; "
            "run uvicorn with --workers 1 and without gunicorn"
        )

    # --- outbound -------------------------------------------------------------
    try:
        ports = settings.allowed_ports
    except ValueError:
        problems.append("REVENANT_UPSTREAM_ALLOWED_PORTS must be a comma-separated list of numbers")
    else:
        if any(port < 1 or port > 65535 for port in ports):
            problems.append("REVENANT_UPSTREAM_ALLOWED_PORTS contains a port outside 1-65535")

    # --- runtime ----------------------------------------------------------------
    if settings.lease_ttl_seconds < (settings.lease_grace_seconds
                                     + settings.request_lifetime_seconds
                                     + settings.lease_clock_margin_seconds):
        problems.append(
            "REVENANT_LEASE_TTL_SECONDS must be at least the grace period plus the request "
            "lifetime plus the clock margin"
        )
    if min(settings.lease_renew_seconds, settings.lease_grace_seconds,
           settings.request_lifetime_seconds, settings.lease_ttl_seconds) <= 0:
        problems.append("lease and request-lifetime settings must be positive")
    elif settings.lease_renew_seconds >= settings.lease_grace_seconds:
        problems.append("REVENANT_LEASE_RENEW_SECONDS must be shorter than the grace period")
    if settings.lease_wait_seconds < settings.lease_ttl_seconds + 10:
        problems.append("REVENANT_LEASE_WAIT_SECONDS must be at least the lease TTL plus 10")
    if settings.upstream_timeout_seconds > settings.request_lifetime_seconds:
        problems.append("REVENANT_UPSTREAM_TIMEOUT_SECONDS must not exceed the request lifetime")
    try:
        proxies = settings.trusted_proxy_networks
    except ValueError:
        problems.append("REVENANT_TRUSTED_PROXIES must be a comma-separated list of networks")
    else:
        if settings.trust_forwarded_host and not proxies:
            problems.append(
                "REVENANT_TRUST_FORWARDED_HOST needs REVENANT_TRUSTED_PROXIES; a forwarded host "
                "is never trusted from an arbitrary peer"
            )
    if settings.trace_queue_size < 1 or settings.runtime_max_concurrency < 1:
        problems.append("trace queue size and runtime concurrency must be at least 1")

    # --- secrets --------------------------------------------------------------
    if secret_key and not _is_placeholder(secret_key) and not is_fernet_key(secret_key):
        problems.append("REVENANT_SECRET_KEY is not a valid Fernet key")
    if previous_key and not is_fernet_key(previous_key):
        problems.append("REVENANT_SECRET_KEY_PREVIOUS is not a valid Fernet key")
    if "*" in settings.cors_origin_list:
        problems.append("REVENANT_CORS_ORIGINS must list explicit origins, not '*'")

    if settings.is_production:
        if not settings.public_host_set:
            problems.append(
                "REVENANT_PUBLIC_HOSTS must list the management hostnames in production"
            )
        if settings.lease_force_acquire:
            problems.append("REVENANT_LEASE_FORCE_ACQUIRE is a development aid and is refused "
                            "in production")
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
