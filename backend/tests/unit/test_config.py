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
