"""Redactor for logs and trace snapshots (requirement 11.3)."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
import structlog

from revenant.logs import configure_logging
from revenant.security.redaction import REDACTED, SECRETS, Redactor, SecretRegistry

LEGACY_KEY = "legacy-demo-key"
TOKEN = "replacement-demo-token"


def test_credential_headers_are_redacted_by_name_in_any_case() -> None:
    redacted = Redactor().headers_map({
        "Authorization": f"Bearer {TOKEN}", "X-API-Key": LEGACY_KEY, "cookie": "session=abc",
        "Set-Cookie": "id=1", "PROXY-AUTHORIZATION": "Basic xyz",
        "Content-Type": "application/json", "X-Request-Id": "r-1",
    })
    assert redacted == {
        "Authorization": REDACTED, "X-API-Key": REDACTED, "cookie": REDACTED,
        "Set-Cookie": REDACTED, "PROXY-AUTHORIZATION": REDACTED,
        "Content-Type": "application/json", "X-Request-Id": "r-1",
    }


def test_adapter_declared_carrier_headers_are_redacted() -> None:
    redacted = Redactor(extra_headers=["X-Legacy-Secret"]).headers_map(
        {"x-legacy-secret": "abc123456", "X-Other": "kept"}
    )
    assert redacted == {"x-legacy-secret": REDACTED, "X-Other": "kept"}


def test_known_secret_values_are_scrubbed_wherever_they_appear() -> None:
    redactor = Redactor(secret_values=[LEGACY_KEY, TOKEN], registry=None)
    snapshot = {
        "url": f"https://api.example/x?api_key={LEGACY_KEY}&page=2",
        "headers": {"X-Forwarded-For": "1.2.3.4", "X-Debug": f"token={TOKEN}"},
        "body": {"note": f"my key is {LEGACY_KEY}", "items": [TOKEN, {"deep": [LEGACY_KEY]}],
                 "amount": 500, "ok": True, "none": None},
        LEGACY_KEY: "a secret used as a key",
    }
    result = redactor.value(snapshot)
    serialised = json.dumps(result)
    assert LEGACY_KEY not in serialised and TOKEN not in serialised
    assert result["url"] == f"https://api.example/x?api_key={REDACTED}&page=2"
    assert result["body"]["amount"] == 500 and result["body"]["ok"] is True
    assert result["body"]["none"] is None
    assert REDACTED in result


def test_percent_encoded_form_of_a_secret_is_scrubbed() -> None:
    secret = "p@ss word/with+chars"
    redactor = Redactor(secret_values=[secret], registry=None)
    encoded = quote(secret, safe="")
    assert redactor.text(f"q={encoded}&x=1") == f"q={REDACTED}&x=1"


def test_longer_secret_is_replaced_whole_when_one_contains_another() -> None:
    redactor = Redactor(secret_values=["abcdef", "abcdefghij"], registry=None)
    assert redactor.text("value abcdefghij end") == f"value {REDACTED} end"


def test_sensitive_paths_are_redacted() -> None:
    redactor = Redactor(sensitive_paths=["$.card.number", "$.items[1].pin", "$.top"],
                        registry=None)
    result = redactor.value({
        "card": {"number": "4111111111111111", "holder": "A"},
        "items": [{"pin": "1111"}, {"pin": "2222"}],
        "top": {"anything": 1},
    })
    assert result == {
        "card": {"number": REDACTED, "holder": "A"},
        "items": [{"pin": "1111"}, {"pin": REDACTED}],
        "top": REDACTED,
    }


def test_redaction_returns_a_copy_and_leaves_the_input_untouched() -> None:
    original = {"a": [LEGACY_KEY], "b": {"c": LEGACY_KEY}}
    result = Redactor(secret_values=[LEGACY_KEY], registry=None).value(original)
    assert original == {"a": [LEGACY_KEY], "b": {"c": LEGACY_KEY}}
    assert result == {"a": [REDACTED], "b": {"c": REDACTED}}


def test_bytes_are_scrubbed_too() -> None:
    redactor = Redactor(secret_values=[TOKEN], registry=None)
    assert TOKEN not in redactor.value(f"Bearer {TOKEN}".encode())


def test_very_short_values_are_not_treated_as_secrets() -> None:
    registry = SecretRegistry()
    registry.register("ab")
    assert Redactor(registry=registry).text("abracadabra") == "abracadabra"


def test_registry_register_forget_and_clear() -> None:
    registry = SecretRegistry()
    registry.register(TOKEN)
    redactor = Redactor(registry=registry)
    assert redactor.text(TOKEN) == REDACTED
    registry.forget(TOKEN)
    assert redactor.text(TOKEN) == TOKEN
    registry.register(TOKEN)
    registry.clear()
    assert registry.values() == frozenset()


def test_log_processor_scrubs_registered_secrets_and_sensitive_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    SECRETS.register(TOKEN)
    SECRETS.register(LEGACY_KEY)
    capsys.readouterr()
    structlog.get_logger("test").info(
        "upstream_call",
        url=f"https://api.novapay.demo/payments?key={LEGACY_KEY}",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
        password="not-registered-but-sensitive-key",
        detail={"nested": [f"x {TOKEN} y"]},
        status=200,
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert TOKEN not in line and LEGACY_KEY not in line
    assert "not-registered-but-sensitive-key" not in line
    assert record["headers"] == {"Authorization": REDACTED, "Accept": "application/json"}
    assert record["password"] == REDACTED
    assert record["status"] == 200 and record["event"] == "upstream_call"


def test_log_processor_handles_unregistered_ordinary_events(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    capsys.readouterr()
    structlog.get_logger("test").info("plain", count=3, name="gravepay")
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["count"] == 3 and record["name"] == "gravepay"
