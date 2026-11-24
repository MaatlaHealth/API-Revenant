"""Admin authentication, CORS and the error envelope (requirement 11.4)."""

from __future__ import annotations

import json

import pytest
import structlog
from fastapi.testclient import TestClient

from revenant.config import Settings
from revenant.main import create_app
from tests.conftest import ADMIN_TOKEN, make_settings

UNAUTHENTICATED = {
    "error": {"code": "unauthenticated", "message": "A valid admin token is required.",
              "details": []}
}


def inbound_url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/credentials/inbound"


# ----------------------------------------------------------------------------- authentication


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": f"Basic {ADMIN_TOKEN}"},
    {"Authorization": ADMIN_TOKEN},
    {"Authorization": f"Bearer {ADMIN_TOKEN}x"},
    {"Authorization": f"Bearer {ADMIN_TOKEN[:-1]}"},
    {"X-Admin-Token": ADMIN_TOKEN},
])
def test_management_api_rejects_missing_or_wrong_token(
    client: TestClient, project_id: str, headers: dict[str, str]
) -> None:
    response = client.get(inbound_url(project_id), headers=headers)
    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED
    assert response.headers["www-authenticate"] == "Bearer"


def test_every_method_of_every_management_route_requires_the_token(
    client: TestClient, project_id: str
) -> None:
    schema = client.get("/api/v1/openapi.json").json()
    checked = 0
    for path, operations in schema["paths"].items():
        url = (path.replace("{project_id}", project_id).replace("{key_id}", "k")
               .replace("{name}", "token").replace("{version}", "1"))
        for method in operations:
            response = client.request(method.upper(), url, json={})
            assert response.status_code == 401, (method, path)
            checked += 1
    assert checked >= 7


def test_correct_token_is_accepted(
    client: TestClient, project_id: str, admin: dict[str, str]
) -> None:
    response = client.get(inbound_url(project_id), headers=admin)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_bearer_scheme_is_case_insensitive(client: TestClient, project_id: str) -> None:
    response = client.get(inbound_url(project_id),
                          headers={"Authorization": f"bearer {ADMIN_TOKEN}"})
    assert response.status_code == 200


def test_unset_admin_token_rejects_everything_including_an_empty_token(
    migrated_url: str, project_id: str
) -> None:
    app = create_app(make_settings(migrated_url, admin_token=""), supervised=False)
    with TestClient(app) as open_client:
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer anything"}):
            assert open_client.get(inbound_url(project_id), headers=headers).status_code == 401


def test_authentication_is_checked_before_the_project_is_looked_up(client: TestClient) -> None:
    """An unauthenticated caller cannot learn which project ids exist."""
    assert client.get(inbound_url("no-such-project")).status_code == 401


def test_rejected_token_is_not_logged(client: TestClient, project_id: str) -> None:
    with structlog.testing.capture_logs() as events:
        client.get(inbound_url(project_id), headers={"Authorization": "Bearer guess-12345678"})
    assert "guess-12345678" not in json.dumps(events, default=str)


# ----------------------------------------------------------------------------- error envelope


def test_not_found_uses_the_envelope(client: TestClient, admin: dict[str, str]) -> None:
    response = client.get(inbound_url("no-such-project"), headers=admin)
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Project not found.", "details": []}
    }


def test_unknown_route_uses_the_envelope(client: TestClient) -> None:
    body = client.get("/api/v1/nothing-here").json()
    assert body["error"]["code"] == "not_found" and body["error"]["details"] == []


def test_wrong_method_uses_the_envelope(client: TestClient) -> None:
    response = client.delete("/healthz")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_validation_errors_name_the_field_but_not_the_submitted_value(
    client: TestClient, project_id: str, admin: dict[str, str]
) -> None:
    secret = "a-secret-the-caller-typed-in-the-wrong-field"
    response = client.post(inbound_url(project_id), headers=admin,
                           json={"label": "", "value": secret, "unexpected": secret})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert {detail["field"] for detail in error["details"]} == {"label", "unexpected"}
    assert secret not in response.text


def test_malformed_json_is_a_validation_error(
    client: TestClient, project_id: str, admin: dict[str, str]
) -> None:
    response = client.post(inbound_url(project_id), content=b"{not json",
                           headers={**admin, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_unexpected_errors_are_reported_without_their_message(settings: Settings) -> None:
    app = create_app(settings, supervised=False)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail with password=hunter2")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred.",
                  "details": []}
    }
    assert "hunter2" not in response.text


def test_database_failure_is_reported_as_unavailable(admin: dict[str, str]) -> None:
    unreachable = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"
    app = create_app(make_settings(unreachable), supervised=False)
    response = TestClient(app, raise_server_exceptions=False).get(
        inbound_url("any"), headers=admin
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "nothing" not in response.text and "127.0.0.1" not in response.text


# ----------------------------------------------------------------------------- CORS


def cors_client(migrated_url: str, origins: str) -> TestClient:
    return TestClient(create_app(make_settings(migrated_url, cors_origins=origins),
                                 supervised=False))


def test_listed_origin_is_allowed(migrated_url: str) -> None:
    client = cors_client(migrated_url, "https://app.example.com, http://localhost:3000/")
    for origin in ("https://app.example.com", "http://localhost:3000"):
        response = client.get("/healthz", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
        assert "access-control-allow-credentials" not in response.headers


def test_unlisted_origin_gets_no_cors_headers(migrated_url: str) -> None:
    client = cors_client(migrated_url, "https://app.example.com")
    for origin in ("https://evil.example.com", "https://app.example.com.evil.com", "null"):
        response = client.get("/healthz", headers={"Origin": origin})
        assert "access-control-allow-origin" not in response.headers


def test_preflight_for_listed_origin_allows_the_authorization_header(migrated_url: str) -> None:
    client = cors_client(migrated_url, "https://app.example.com")
    response = client.options("/api/v1/projects/x/credentials/inbound", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_no_cors_headers_when_no_origins_are_configured(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in response.headers
