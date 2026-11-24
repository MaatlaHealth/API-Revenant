"""Liveness, readiness and the request log (requirements 12.2 and 6.18)."""

from __future__ import annotations

import json

import pytest
import structlog
from fastapi.testclient import TestClient

from revenant import __version__
from revenant.config import Settings
from revenant.logs import configure_logging, error_fields
from revenant.main import create_app

SECRET_IN_QUERY = "k3y-that-must-never-be-logged"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None)))  # type: ignore[call-arg]


def test_healthz_reports_liveness(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_readiness(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"configuration": "ok"}}


def test_unknown_path_is_not_found(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404


def test_openapi_document_is_served_under_api_v1(client: TestClient) -> None:
    document = client.get("/api/v1/openapi.json").json()
    assert document["info"]["version"] == __version__
    assert "/healthz" not in document["paths"]


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    first = client.get("/healthz").headers["x-request-id"]
    second = client.get("/healthz").headers["x-request-id"]
    assert len(first) == 32 and first != second


def test_request_log_records_path_without_query_string(client: TestClient) -> None:
    with structlog.testing.capture_logs() as events:
        response = client.get(f"/healthz?api_key={SECRET_IN_QUERY}&page=2")
    assert response.status_code == 200
    request_events = [event for event in events if event["event"] == "http_request"]
    assert len(request_events) == 1
    event = request_events[0]
    assert event["method"] == "GET"
    assert event["path"] == "/healthz"
    assert event["status"] == 200
    assert event["request_id"] == response.headers["x-request-id"]
    assert isinstance(event["duration_ms"], float)
    serialised = json.dumps(events, default=str)
    assert SECRET_IN_QUERY not in serialised
    assert "api_key" not in serialised and "page=2" not in serialised


def test_request_log_for_not_found_also_omits_query(client: TestClient) -> None:
    with structlog.testing.capture_logs() as events:
        client.get(f"/missing?token={SECRET_IN_QUERY}")
    assert SECRET_IN_QUERY not in json.dumps(events, default=str)
    assert events[-1]["status"] == 404 and events[-1]["path"] == "/missing"


def test_failed_request_is_logged_by_error_type_not_message() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"upstream said {SECRET_IN_QUERY}")

    with structlog.testing.capture_logs() as events:
        with pytest.raises(RuntimeError):
            TestClient(app).get(f"/boom?api_key={SECRET_IN_QUERY}")
    failures = [event for event in events if event["event"] == "http_request_failed"]
    assert len(failures) == 1
    assert failures[0]["error_type"] == "RuntimeError"
    assert failures[0]["path"] == "/boom"
    assert SECRET_IN_QUERY not in json.dumps(events, default=str)


def test_error_fields_contain_only_the_type() -> None:
    assert error_fields(ValueError("secret detail")) == {"error_type": "ValueError"}


def test_log_output_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.get_logger("test").info("hello", answer=42)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "hello" and record["answer"] == 42 and record["level"] == "info"


def test_startup_log_does_not_contain_secret_values(capsys: pytest.CaptureFixture[str]) -> None:
    token = "t" * 40
    create_app(Settings(_env_file=None, admin_token=token))  # type: ignore[call-arg]
    assert token not in capsys.readouterr().out
