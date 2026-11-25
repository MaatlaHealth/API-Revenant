"""Projects API (requirement 1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from revenant.db.models import Project
from revenant.db.session import Database

URL = "/api/v1/projects"
BODY = {"name": "GravePay resurrection", "legacy_name": "GravePay", "replacement_name": "NovaPay"}


def resolver_returning(*addresses: str):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)
    return resolve


def create(client: TestClient, admin: dict[str, str], **overrides: object) -> dict:
    response = client.post(URL, headers=admin, json={**BODY, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_project_generates_a_url_safe_slug(
    client: TestClient, admin: dict[str, str]
) -> None:
    project = create(client, admin)
    assert project["slug"] == "gravepay-resurrection"
    assert project["status"] == "draft" and project["is_demo"] is False
    assert project["evidence_version"] == 0
    assert project["evidence"] == {"files": 0, "legacy_files": 0, "replacement_files": 0,
                                   "failed_files": 0, "endpoints": 0, "fixtures": 0}


@pytest.mark.parametrize("name, slug", [
    ("Café Münchën API!", "cafe-munchen-api"),
    ("  --Lots   of   spaces--  ", "lots-of-spaces"),
    ("日本語", "project"),
    ("x" * 200, "x" * 64),
])
def test_slugs_are_derived_safely_from_any_name(
    client: TestClient, admin: dict[str, str], name: str, slug: str
) -> None:
    assert create(client, admin, name=name)["slug"] == slug


def test_generated_slugs_are_made_unique(client: TestClient, admin: dict[str, str]) -> None:
    slugs = [create(client, admin)["slug"] for _ in range(3)]
    assert slugs == ["gravepay-resurrection", "gravepay-resurrection-2",
                     "gravepay-resurrection-3"]


def test_supplied_duplicate_slug_is_a_conflict_naming_the_field(
    client: TestClient, admin: dict[str, str]
) -> None:
    create(client, admin, slug="gravepay")
    response = client.post(URL, headers=admin, json={**BODY, "slug": "gravepay"})
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "conflict", "message": "A project with this slug already exists.",
        "details": [{"field": "slug", "issue": "already exists"}]}


@pytest.mark.parametrize("slug", ["Has-Caps", "-leading", "trailing-", "has space", "a/b",
                                  "x" * 65, "", "runtime/../x"])
def test_invalid_slugs_are_rejected(client: TestClient, admin: dict[str, str], slug: str) -> None:
    response = client.post(URL, headers=admin, json={**BODY, "slug": slug})
    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "slug"


@pytest.mark.parametrize("body", [
    {}, {"name": "", "legacy_name": "A", "replacement_name": "B"},
    {"name": "N", "legacy_name": "A"}, {**BODY, "unexpected": True},
    {**BODY, "name": "x" * 201},
])
def test_invalid_project_bodies_are_rejected(
    client: TestClient, admin: dict[str, str], body: dict
) -> None:
    assert client.post(URL, headers=admin, json=body).status_code == 400


def test_get_list_and_pagination(client: TestClient, admin: dict[str, str]) -> None:
    ids = [create(client, admin, name=f"Project {i}")["id"] for i in range(3)]
    listing = client.get(URL, headers=admin).json()
    assert listing["total"] == 3 and {item["id"] for item in listing["items"]} == set(ids)
    page = client.get(f"{URL}?limit=2&offset=2", headers=admin).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get(f"{URL}/{ids[0]}", headers=admin).json()["name"] == "Project 0"
    assert client.get(f"{URL}/nope", headers=admin).status_code == 404
    assert client.get(f"{URL}?limit=0", headers=admin).status_code == 400
    assert client.get(f"{URL}?limit=500", headers=admin).status_code == 400


def test_workflow_shows_what_is_done_and_what_blocks_the_next_step(
    client: TestClient, admin: dict[str, str]
) -> None:
    steps = {step["key"]: step for step in create(client, admin)["workflow"]}
    assert list(steps) == ["legacy_specification", "replacement_specification", "fixtures",
                           "reconstruction", "adapter", "verification", "runtime"]
    assert all(not step["complete"] for step in steps.values())
    assert steps["legacy_specification"]["available"] is True
    assert steps["reconstruction"]["available"] is False
    assert steps["reconstruction"]["blocked_by"] == "Import the legacy specification first."
    assert steps["runtime"]["blocked_by"] == "Requires the previous step."


def test_update_project(client: TestClient, admin: dict[str, str]) -> None:
    project = create(client, admin)
    updated = client.patch(f"{URL}/{project['id']}", headers=admin,
                           json={"name": "Renamed", "description": "New text"}).json()
    assert updated["name"] == "Renamed" and updated["description"] == "New text"
    assert updated["slug"] == project["slug"]  # the slug is part of runtime URLs; it is stable
    assert client.patch(f"{URL}/{project['id']}", headers=admin,
                        json={"slug": "other"}).status_code == 400


def test_delete_project_and_refusal_while_running(
    client: TestClient, admin: dict[str, str], database: Database
) -> None:
    project = create(client, admin)
    with database.session() as session:
        session.get(Project, project["id"]).status = "running"
    refused = client.delete(f"{URL}/{project['id']}", headers=admin)
    assert refused.status_code == 409
    assert "Stop it before deleting" in refused.json()["error"]["message"]
    with database.session() as session:
        session.get(Project, project["id"]).status = "draft"
    assert client.delete(f"{URL}/{project['id']}", headers=admin).status_code == 204
    assert client.get(f"{URL}/{project['id']}", headers=admin).status_code == 404


def test_activity_lists_audit_records_newest_first(
    client: TestClient, admin: dict[str, str]
) -> None:
    project = create(client, admin)
    client.patch(f"{URL}/{project['id']}", headers=admin, json={"name": "Renamed"})
    activity = client.get(f"{URL}/{project['id']}/activity", headers=admin).json()
    assert [item["action"] for item in activity["items"]] == ["project.update", "project.create"]
    assert activity["items"][0]["metadata"] == {"fields": ["name"]}
    assert activity["items"][0]["actor"] == "admin"


# ----------------------------------------------------------------------------- replacement URL


def test_public_replacement_url_is_accepted_and_normalised(
    client: TestClient, admin: dict[str, str]
) -> None:
    client.app.state.resolver = resolver_returning("93.184.216.34")
    project = create(client, admin, replacement_base_url="https://api.novapay.example/")
    assert project["replacement_base_url"] == "https://api.novapay.example"


@pytest.mark.parametrize("url, addresses", [
    ("http://169.254.169.254/latest/meta-data", ["93.184.216.34"]),
    ("http://127.0.0.1:8000", ["93.184.216.34"]),
    ("https://internal.example.com", ["10.0.0.5"]),
    ("https://rebind.example.com", ["93.184.216.34", "127.0.0.1"]),
    ("ftp://api.example.com", ["93.184.216.34"]),
    ("https://user:secret-pass@api.example.com", ["93.184.216.34"]),
    ("https://api.example.com:22", ["93.184.216.34"]),
    ("not a url", ["93.184.216.34"]),
])
def test_unsafe_replacement_urls_are_rejected_before_saving(
    client: TestClient, admin: dict[str, str], url: str, addresses: list[str]
) -> None:
    client.app.state.resolver = resolver_returning(*addresses)
    response = client.post(URL, headers=admin, json={**BODY, "replacement_base_url": url})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsafe_url"
    assert "secret-pass" not in response.text
    assert client.get(URL, headers=admin).json()["total"] == 0


def test_unsafe_url_is_also_rejected_on_update(client: TestClient, admin: dict[str, str]) -> None:
    client.app.state.resolver = resolver_returning("93.184.216.34")
    project = create(client, admin, replacement_base_url="https://api.novapay.example")
    client.app.state.resolver = resolver_returning("192.168.1.10")
    response = client.patch(f"{URL}/{project['id']}", headers=admin,
                            json={"replacement_base_url": "https://moved.example.com"})
    assert response.status_code == 400
    unchanged = client.get(f"{URL}/{project['id']}", headers=admin).json()
    assert unchanged["replacement_base_url"] == "https://api.novapay.example"


def test_url_that_does_not_resolve_yet_is_accepted(
    client: TestClient, admin: dict[str, str]
) -> None:
    async def unresolvable(host: str, port: int) -> list[str]:
        raise OSError("name not known")

    client.app.state.resolver = unresolvable
    project = create(client, admin, replacement_base_url="http://api.novapay.demo:8000")
    assert project["replacement_base_url"] == "http://api.novapay.demo:8000"


def test_replacement_url_can_be_cleared(client: TestClient, admin: dict[str, str]) -> None:
    client.app.state.resolver = resolver_returning("93.184.216.34")
    project = create(client, admin, replacement_base_url="https://api.novapay.example")
    cleared = client.patch(f"{URL}/{project['id']}", headers=admin,
                           json={"replacement_base_url": None}).json()
    assert cleared["replacement_base_url"] is None
