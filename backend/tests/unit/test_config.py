"""Start-up configuration checks (requirement 11.5)."""

from __future__ import annotations

import base64

import pytest

from revenant.config import Settings, startup_problems
from revenant.errors import ConfigurationError
from revenant.main import create_app

VALID_KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
VALID_TOKEN = "t" * 40


def settings(**overrides: object) -> Settings:
    # Production must name its management hosts; the tests below that are about
    # other rules supply one, and the rule has its own test.
    if overrides.get("env") == "production":
        overrides.setdefault("public_hosts", "api.example.com")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_development_defaults_start() -> None:
    assert startup_problems(settings()) == []


def test_environment_variables_are_read_with_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVENANT_ENV", "demo")
    monkeypatch.setenv("REVENANT_UPSTREAM_ALLOWLIST", "api.novapay.demo")
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.env == "demo"
    assert loaded.allowlisted_hosts == frozenset({"api.novapay.demo"})


def test_production_with_generated_values_starts() -> None:
    assert startup_problems(
        settings(env="production", secret_key=VALID_KEY, admin_token=VALID_TOKEN)
    ) == []


@pytest.mark.parametrize("overrides, expected", [
    ({"secret_key": "", "admin_token": VALID_TOKEN}, "REVENANT_SECRET_KEY"),
    ({"secret_key": "CHANGE_ME_GENERATE_A_FERNET_KEY", "admin_token": VALID_TOKEN},
     "REVENANT_SECRET_KEY"),
    ({"secret_key": VALID_KEY, "admin_token": ""}, "REVENANT_ADMIN_TOKEN"),
    ({"secret_key": VALID_KEY, "admin_token": "CHANGE_ME_GENERATE_A_TOKEN"},
     "REVENANT_ADMIN_TOKEN"),
    ({"secret_key": VALID_KEY, "admin_token": "short"}, "at least"),
    ({"secret_key": VALID_KEY, "admin_token": VALID_TOKEN, "allow_private_upstreams": True},
     "REVENANT_ALLOW_PRIVATE_UPSTREAMS"),
])
def test_production_refuses_example_secrets_and_unsafe_flags(
    overrides: dict[str, object], expected: str
) -> None:
    problems = startup_problems(settings(env="production", **overrides))
    assert any(expected in problem for problem in problems), problems


def test_all_problems_are_reported_together() -> None:
    problems = startup_problems(settings(env="production", allow_private_upstreams=True))
    assert len(problems) == 3


def test_malformed_secret_key_is_rejected_in_every_environment() -> None:
    problems = startup_problems(settings(secret_key="not-a-fernet-key"))
    assert problems == ["REVENANT_SECRET_KEY is not a valid Fernet key"]


@pytest.mark.parametrize("ports", ["80,abc", "0", "70000", "443,-1"])
def test_invalid_port_list_is_rejected(ports: str) -> None:
    assert startup_problems(settings(upstream_allowed_ports=ports))


def test_private_upstreams_are_allowed_outside_production() -> None:
    assert startup_problems(settings(env="demo", allow_private_upstreams=True)) == []


def test_application_refuses_to_build_with_unsafe_configuration() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        create_app(settings(env="production"))
    assert len(excinfo.value.problems) == 2


def test_problem_messages_never_contain_secret_values() -> None:
    secret = "super-secret-value-that-is-not-a-fernet-key"
    problems = startup_problems(settings(env="production", secret_key=secret, admin_token="short"))
    assert problems
    assert all(secret not in problem and "short" not in problem for problem in problems)


def test_secrets_are_not_exposed_by_repr() -> None:
    text = repr(settings(secret_key=VALID_KEY, admin_token=VALID_TOKEN))
    assert VALID_KEY not in text and VALID_TOKEN not in text


# ----------------------------------------------------------------------------- single worker


@pytest.mark.parametrize("overrides", [
    {"web_concurrency": 2}, {"web_concurrency": 0}, {"workers": 4},
    {"web_concurrency": 1, "workers": 2},
])
def test_more_than_one_worker_is_refused(overrides: dict[str, object]) -> None:
    problems = startup_problems(settings(**overrides), supervised=False)
    assert problems and all(p.startswith("unsupported_multi_worker") for p in problems)


@pytest.mark.parametrize("overrides", [{}, {"web_concurrency": 1}, {"workers": 1},
                                       {"web_concurrency": 1, "workers": 1}])
def test_exactly_one_worker_starts(overrides: dict[str, object]) -> None:
    assert startup_problems(settings(**overrides), supervised=False) == []


def test_web_concurrency_is_read_from_the_unprefixed_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.web_concurrency == 3
    assert startup_problems(loaded, supervised=False)[0].startswith("unsupported_multi_worker")


@pytest.mark.parametrize("env", ["demo", "production"])
def test_multi_process_supervisor_is_refused_outside_development(env: str) -> None:
    configured = settings(env=env, secret_key=VALID_KEY, admin_token=VALID_TOKEN)
    problems = startup_problems(configured, supervised=True)
    assert len(problems) == 1 and problems[0].startswith("unsupported_multi_worker")
    assert startup_problems(configured, supervised=False) == []


def test_reloader_child_process_is_allowed_in_development() -> None:
    assert startup_problems(settings(), supervised=True) == []


def test_gunicorn_is_detected_as_a_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    from revenant.config import under_multiprocess_supervisor

    assert under_multiprocess_supervisor() is False
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/23.0.0")
    assert under_multiprocess_supervisor() is True


def test_application_refuses_to_build_with_two_workers() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        create_app(settings(web_concurrency=2), supervised=False)
    assert "unsupported_multi_worker" in str(excinfo.value)


# ----------------------------------------------------------------------------- other settings


def test_database_url_is_read_from_the_unprefixed_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/d")
    assert Settings(_env_file=None).database_url == "postgres://u:p@h/d"  # type: ignore[call-arg]


def test_wildcard_cors_origin_is_refused() -> None:
    problems = startup_problems(settings(cors_origins="https://app.example.com, *"),
                                supervised=False)
    assert problems == ["REVENANT_CORS_ORIGINS must list explicit origins, not '*'"]


def test_malformed_previous_secret_key_is_refused() -> None:
    problems = startup_problems(settings(secret_key=VALID_KEY, secret_key_previous="nope"),
                                supervised=False)
    assert problems == ["REVENANT_SECRET_KEY_PREVIOUS is not a valid Fernet key"]


# ---- runtime settings (requirements 7.23, 7.31) ----------------------------------------------


def test_production_must_list_its_management_hosts() -> None:
    problems = startup_problems(settings(env="production", secret_key=VALID_KEY,
                                         admin_token=VALID_TOKEN, public_hosts=" "),
                                supervised=False)
    assert problems == ["REVENANT_PUBLIC_HOSTS must list the management hostnames in production"]


@pytest.mark.parametrize("overrides", [
    {"lease_ttl_seconds": 89},                                   # 45 + 30 + 15 = 90
    {"lease_grace_seconds": 46},
    {"request_lifetime_seconds": 31, "upstream_timeout_seconds": 5},
    {"lease_clock_margin_seconds": 16},
])
def test_the_lease_ttl_must_cover_grace_lifetime_and_margin(overrides: dict[str, float]) -> None:
    problems = startup_problems(settings(**overrides), supervised=False)
    assert problems == ["REVENANT_LEASE_TTL_SECONDS must be at least the grace period plus the "
                        "request lifetime plus the clock margin"]


def test_the_default_lease_timing_is_exactly_at_the_limit() -> None:
    defaults = settings()
    assert defaults.lease_ttl_seconds == (defaults.lease_grace_seconds
                                          + defaults.request_lifetime_seconds
                                          + defaults.lease_clock_margin_seconds)
    assert startup_problems(settings(lease_ttl_seconds=90), supervised=False) == []


def test_other_runtime_settings_are_checked() -> None:
    def problems(**overrides: object) -> str:
        return " | ".join(startup_problems(settings(**overrides), supervised=False))

    assert "at least the lease TTL plus 10" in problems(lease_wait_seconds=95)
    assert "shorter than the grace period" in problems(lease_renew_seconds=45)
    assert "must be positive" in problems(lease_renew_seconds=0)
    assert "must not exceed the request lifetime" in problems(upstream_timeout_seconds=31)
    assert "needs REVENANT_TRUSTED_PROXIES" in problems(trust_forwarded_host=True)
    assert problems(trust_forwarded_host=True, trusted_proxies="10.0.0.0/8") == ""
    assert "list of networks" in problems(trusted_proxies="not-a-network")
    assert "must be at least 1" in problems(trace_queue_size=0)


def test_force_acquire_is_refused_in_production() -> None:
    problems = startup_problems(settings(env="production", secret_key=VALID_KEY,
                                         admin_token=VALID_TOKEN, lease_force_acquire=True),
                                supervised=False)
    assert problems == ["REVENANT_LEASE_FORCE_ACQUIRE is a development aid and is refused in "
                        "production"]
    assert startup_problems(settings(lease_force_acquire=True), supervised=False) == []
