"""Evidence Vault API on the demonstration evidence (requirements 2 and 9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from revenant.db.session import Database
from revenant.main import create_app
from tests.conftest import make_settings

DEMO = Path(__file__).resolve().parents[3] / "demo"
FILES = {
    "legacy_spec": (DEMO / "gravepay" / "openapi.yaml", "legacy"),
    "legacy_fixtures": (DEMO / "gravepay" / "fixtures.json", "legacy"),
    "legacy_notes": (DEMO / "gravepay" / "notes.md", "legacy"),
    "replacement_spec": (DEMO / "novapay" / "openapi.yaml", "replacement"),
    "recordings": (DEMO / "novapay" / "recordings.json", "replacement"),
}


def base(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}"


def upload(client: TestClient, admin: dict[str, str], project_id: str, filename: str,
           content: bytes, side: str, **form: str):
    return client.post(f"{base(project_id)}/evidence", headers=admin,
                       files={"file": (filename, content, "application/octet-stream")},
                       data={"side": side, **form})


def upload_demo(client: TestClient, admin: dict[str, str], project_id: str, key: str) -> dict:
    path, side = FILES[key]
    response = upload(client, admin, project_id, path.name, path.read_bytes(), side)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def demo(client: TestClient, admin: dict[str, str], project_id: str) -> dict[str, dict]:
    """The whole demonstration evidence set, imported through the API."""
    return {key: upload_demo(client, admin, project_id, key) for key in FILES}


def get(client: TestClient, admin: dict[str, str], project_id: str, path: str) -> dict:
    response = client.get(f"{base(project_id)}{path}", headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def endpoint_id(client, admin, project_id: str, side: str, method: str, path: str) -> str:
    items = get(client, admin, project_id, f"/endpoints?side={side}")["items"]
    return next(i["id"] for i in items if (i["method"], i["path_template"]) == (method, path))


# ============================================================================= import


def test_demo_evidence_imports_and_reports_what_was_parsed(demo: dict[str, dict]) -> None:
    assert demo["legacy_spec"]["kind"] == "openapi"
    assert demo["legacy_spec"]["parse_status"] == "parsed"
    assert demo["legacy_spec"]["parse_report"]["endpoints"] == 6
    assert demo["legacy_spec"]["fact_count"] == demo["legacy_spec"]["parse_report"]["facts"] > 100
    assert demo["replacement_spec"]["parse_report"]["endpoints"] == 6
    assert demo["legacy_fixtures"]["kind"] == "fixtures"
    assert demo["legacy_fixtures"]["parse_status"] == "parsed"
    assert demo["legacy_fixtures"]["parse_report"]["fixtures"] == 10
    assert demo["recordings"]["parse_status"] == "parsed"
    assert demo["legacy_notes"]["kind"] == "markdown"
    assert demo["legacy_notes"]["parse_status"] == "unsupported"
    assert "not analysed" in demo["legacy_notes"]["parse_report"]["warnings"][0]


def test_project_summary_reflects_the_imported_evidence(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    project = get(client, admin, project_id, "")
    assert project["status"] == "evidence"
    assert project["evidence_version"] == 5
    assert project["evidence"] == {"files": 5, "legacy_files": 3, "replacement_files": 2,
                                   "failed_files": 0, "endpoints": 12, "fixtures": 20}
    steps = {step["key"]: step for step in project["workflow"]}
    assert steps["legacy_specification"]["complete"] is True
    assert steps["replacement_specification"]["complete"] is True
    assert steps["fixtures"]["complete"] is True
    # Reconstruction can now be run; it has not been, so later steps wait for it.
    assert steps["reconstruction"] == {
        "key": "reconstruction", "title": "Reconstruct and review mappings",
        "complete": False, "available": True, "blocked_by": None}
    assert steps["adapter"]["blocked_by"] == "Requires the previous step."


def test_endpoints_are_listed_per_side(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    legacy = get(client, admin, project_id, "/endpoints?side=legacy")
    assert sorted((i["method"], i["path_template"]) for i in legacy["items"]) == [
        ("GET", "/v1/charges"), ("GET", "/v1/charges/{id}"), ("GET", "/v1/charges/{id}/events"),
        ("GET", "/v1/charges/{id}/receipt"), ("POST", "/v1/charges"),
        ("POST", "/v1/charges/{id}/refund")]
    assert all(item["from_specification"] for item in legacy["items"])
    assert get(client, admin, project_id, "/endpoints?side=replacement")["total"] == 6
    assert get(client, admin, project_id, "/endpoints")["total"] == 12
    assert client.get(f"{base(project_id)}/endpoints?side=other",
                      headers=admin).status_code == 400


def test_every_fact_records_its_evidence_file_and_location(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    create = endpoint_id(client, admin, project_id, "legacy", "POST", "/v1/charges")
    facts = get(client, admin, project_id, f"/endpoints/{create}/facts")["items"]
    assert len(facts) > 20
    for fact in facts:
        assert fact["evidence_file"]["filename"] in ("openapi.yaml", "fixtures.json")
        assert fact["locator"].startswith(("#/", "fixture:"))
        assert fact["basis"] in ("declared", "exemplified")
    customer = next(f for f in facts if f["subject"] == "request:$.customer")
    assert customer["value"] == {"type": "string", "required": True}
    assert customer["locator"] == "#/components/schemas/ChargeRequest/properties/customer"
    assert customer["evidence_file"]["id"] == demo["legacy_spec"]["id"]


def test_the_same_fact_from_two_sources_has_two_provenance_rows(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    create = endpoint_id(client, admin, project_id, "legacy", "POST", "/v1/charges")
    facts = get(client, admin, project_id, f"/endpoints/{create}/facts")["items"]
    created = [f for f in facts if f["fact_type"] == "status_code" and f["subject"] == "201"]
    assert sorted(f["basis"] for f in created) == ["declared", "exemplified", "exemplified"]
    examples = sorted(f["locator"] for f in created if f["basis"] == "exemplified")
    assert examples == ["fixture:create charge - USD", "fixture:create charge - ZAR"]


def test_fixtures_are_associated_with_the_matching_endpoint(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    fixtures = get(client, admin, project_id, "/fixtures")["items"]
    assert len(fixtures) == 20
    by_name = {item["name"]: item for item in fixtures}
    retrieve = endpoint_id(client, admin, project_id, "legacy", "GET", "/v1/charges/{id}")
    receipt = endpoint_id(client, admin, project_id, "legacy", "GET", "/v1/charges/{id}/receipt")
    assert by_name["retrieve charge"]["endpoint_id"] == retrieve
    assert by_name["retrieve charge - not found"]["endpoint_id"] == retrieve
    assert by_name["charge receipt"]["endpoint_id"] == receipt
    assert by_name["create charge - ZAR"]["variable_fields"] == ["$.id", "$.created"]
    assert all(item["endpoint_id"] for item in fixtures)


def test_recordings_are_paired_with_their_legacy_fixture(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    fixtures = get(client, admin, project_id, "/fixtures")["items"]
    legacy = {item["id"]: item["name"] for item in fixtures if item["side"] == "legacy"}
    recordings = [item for item in fixtures if item["side"] == "replacement"]
    assert len(recordings) == 10
    for recording in recordings:
        assert legacy[recording["pair_with"]] == recording["pair_with_name"]


def test_import_order_does_not_matter(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    """Recordings and fixtures imported before either specification still end up linked."""
    for key in ("recordings", "legacy_fixtures"):
        upload_demo(client, admin, project_id, key)
    early = get(client, admin, project_id, "/endpoints?side=legacy")["items"]
    assert early and all(not item["from_specification"] for item in early)
    assert ("GET", "/v1/charges/P-1002") in {(i["method"], i["path_template"]) for i in early}
    for key in ("replacement_spec", "legacy_spec"):
        upload_demo(client, admin, project_id, key)
    late = get(client, admin, project_id, "/endpoints?side=legacy")["items"]
    assert len(late) == 6 and all(item["from_specification"] for item in late)
    fixtures = get(client, admin, project_id, "/fixtures")["items"]
    assert all(item["pair_with"] for item in fixtures if item["side"] == "replacement")


# ============================================================================= missing evidence


def test_missing_evidence_lists_exactly_what_the_demo_leaves_open(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    items = get(client, admin, project_id, "/evidence/missing")["items"]
    by_code: dict[str, list[str]] = {}
    for item in items:
        by_code.setdefault(item["code"], []).append(item["message"])
    assert set(by_code) == {"status_without_example", "endpoint_without_fixture"}
    assert by_code["endpoint_without_fixture"] == [
        "GET /v1/charges/{id}/events has no historical fixture."]
    assert sorted(by_code["status_without_example"]) == sorted([
        "GET /v1/charges documents status 401 but no fixture shows it.",
        "GET /v1/charges/{id}/events documents status 200 but no fixture shows it.",
        "GET /v1/charges/{id}/receipt documents status 404 but no fixture shows it.",
        "POST /v1/charges documents status 401 but no fixture shows it.",
        "POST /v1/charges documents status 429 but no fixture shows it.",
        "POST /v1/charges/{id}/refund documents status 404 but no fixture shows it.",
        "POST /v1/charges/{id}/refund documents status 409 but no fixture shows it.",
    ])


def test_missing_evidence_for_an_empty_project(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    items = get(client, admin, project_id, "/evidence/missing")["items"]
    assert sorted((item["code"], item["side"]) for item in items) == [
        ("no_legacy_fixtures", "legacy"), ("no_specification", "legacy"),
        ("no_specification", "replacement")]


def test_unpaired_fixtures_and_fixture_only_endpoints_are_reported(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    upload_demo(client, admin, project_id, "legacy_fixtures")
    recording = {"revenant_fixtures": "1.0", "fixtures": [{
        "name": "orphan", "pair_with": "no such legacy fixture",
        "request": {"method": "GET", "path": "/payments/P-1"},
        "response": {"status": 200, "body": {}}}]}
    response = upload(client, admin, project_id, "orphan.json",
                      json.dumps(recording).encode(), "replacement")
    assert response.json()["parse_status"] == "partial"
    assert "pair_with names no legacy fixture" in response.json()["parse_report"]["warnings"][0]
    codes = {item["code"] for item in get(client, admin, project_id, "/evidence/missing")["items"]}
    assert {"fixture_without_recording", "recording_without_fixture",
            "endpoint_without_specification"} <= codes


# ============================================================================= nothing is discarded


@pytest.mark.parametrize("filename, content, problem", [
    ("broken.json", b'{"openapi": "3.0.3", "paths": ', "not valid JSON"),
    ("broken.yaml", b"openapi: [unclosed\npaths: }", "not valid YAML"),
    ("mystery.json", b'{"hello": "world"}', "not recognised"),
    ("empty-spec.yaml", b"openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths: {}\n",
     "no operations"),
    ("bad-version.json", b'{"revenant_fixtures": "7.0", "fixtures": []}', "unsupported fixture"),
    ("no-valid.json", b'{"revenant_fixtures": "1.0", "fixtures": [{"name": ""}]}',
     "no valid fixtures"),
])
def test_unparseable_files_are_stored_with_a_report_not_discarded(
    client: TestClient, admin: dict[str, str], project_id: str,
    filename: str, content: bytes, problem: str,
) -> None:
    response = upload(client, admin, project_id, filename, content, "legacy")
    assert response.status_code == 201
    body = response.json()
    assert body["parse_status"] == "failed"
    assert any(problem in item for item in body["parse_report"]["problems"])
    listing = get(client, admin, project_id, "/evidence")
    assert [item["filename"] for item in listing["items"]] == [filename]
    assert get(client, admin, project_id, "")["evidence"]["failed_files"] == 1
    codes = [i["code"] for i in get(client, admin, project_id, "/evidence/missing")["items"]]
    assert "file_not_parsed" in codes


def test_partially_valid_fixture_file_keeps_the_good_and_reports_the_bad(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    document = {"revenant_fixtures": "1.0", "fixtures": [
        {"name": "ok", "request": {"method": "GET", "path": "/a"}, "response": {"status": 200}},
        {"name": "bad", "request": {"method": "NOPE", "path": "/a"}, "response": {"status": 200}},
    ]}
    body = upload(client, admin, project_id, "mixed.json", json.dumps(document).encode(),
                  "legacy").json()
    assert body["parse_status"] == "partial"
    assert body["parse_report"]["fixtures"] == 1
    assert body["parse_report"]["rejected"] == [
        {"index": "1", "reason": "fixture 'bad': request.method must be an HTTP method"}]
    assert get(client, admin, project_id, "/fixtures")["total"] == 1


def test_unresolved_references_make_a_specification_partial(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    spec = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {"/a": {"get": {
        "responses": {"200": {"description": "ok", "content": {"application/json": {
            "schema": {"$ref": "https://example.com/remote.json"}}}}}}}}}
    body = upload(client, admin, project_id, "remote.json", json.dumps(spec).encode(),
                  "legacy").json()
    assert body["parse_status"] == "partial"
    assert "not fetched" in body["parse_report"]["unresolved_refs"][0]
    codes = [i["code"] for i in get(client, admin, project_id, "/evidence/missing")["items"]]
    assert "unresolved_reference" in codes


# ============================================================================= rejected uploads


def test_disallowed_type_is_rejected_with_a_specific_error(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    response = upload(client, admin, project_id, "malware.exe", b"MZ\x90\x00", "legacy")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_evidence"
    assert get(client, admin, project_id, "/evidence")["total"] == 0


def test_oversized_file_is_rejected(migrated_url: str, admin: dict[str, str],
                                    project_id: str) -> None:
    app = create_app(make_settings(migrated_url, max_upload_bytes=1000), supervised=False)
    with TestClient(app) as small:
        response = upload(small, admin, project_id, "big.json", b"{" + b" " * 2000 + b"}",
                          "legacy")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
        assert get(small, admin, project_id, "/evidence")["total"] == 0


def test_file_limit_per_project(migrated_url: str, admin: dict[str, str], project_id: str) -> None:
    app = create_app(make_settings(migrated_url, max_evidence_files=2), supervised=False)
    with TestClient(app) as limited:
        for index in range(2):
            assert upload(limited, admin, project_id, f"n{index}.md", f"note {index}".encode(),
                          "legacy").status_code == 201
        response = upload(limited, admin, project_id, "n3.md", b"note 3", "legacy")
        assert response.status_code == 400
        assert "at most 2" in response.json()["error"]["message"]


def test_side_is_required_and_validated(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    files = {"file": ("a.md", b"text", "text/markdown")}
    url = f"{base(project_id)}/evidence"
    assert client.post(url, headers=admin, files=files).status_code == 400
    assert client.post(url, headers=admin, files=files,
                       data={"side": "sideways"}).status_code == 400


def test_declared_kind_must_match_the_content(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    path, side = FILES["legacy_fixtures"]
    body = upload(client, admin, project_id, path.name, path.read_bytes(), side,
                  kind="openapi").json()
    assert body["parse_status"] == "failed"
    assert body["parse_report"]["problems"] == ["declared as openapi but the content is fixtures"]


def test_duplicate_import_is_a_conflict(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    upload_demo(client, admin, project_id, "legacy_spec")
    path, side = FILES["legacy_spec"]
    again = upload(client, admin, project_id, "renamed.yaml", path.read_bytes(), side)
    assert again.status_code == 409
    assert again.json()["error"]["details"][0]["issue"] == "identical to openapi.yaml"


def test_uploaded_path_components_are_discarded(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    body = upload(client, admin, project_id, "../../../etc/cron.d/notes.md", b"text",
                  "legacy").json()
    assert body["filename"] == "notes.md"


def test_evidence_routes_require_the_admin_token(client: TestClient, project_id: str) -> None:
    for path in ("/evidence", "/evidence/missing", "/endpoints", "/fixtures"):
        assert client.get(f"{base(project_id)}{path}").status_code == 401
    assert client.post(f"{base(project_id)}/evidence",
                       files={"file": ("a.md", b"x", "text/plain")},
                       data={"side": "legacy"}).status_code == 401


# ============================================================================= fixture credentials


def test_credentials_in_uploaded_fixtures_are_not_stored_anywhere(
    client: TestClient, admin: dict[str, str], project_id: str, database: Database
) -> None:
    secrets = ["live-key-AAAA1111", "bearer-BBBB2222", "query-CCCC3333"]
    document = {"revenant_fixtures": "1.0", "fixtures": [{
        "name": "with credentials",
        "request": {"method": "GET", "path": "/v1/charges",
                    "query": {"api_key": secrets[2], "page": 1},
                    "headers": {"X-API-Key": secrets[0], "Authorization": f"Bearer {secrets[1]}"}},
        "response": {"status": 200, "body": {"data": []}}}]}
    body = upload(client, admin, project_id, "creds.json", json.dumps(document).encode(),
                  "legacy").json()
    assert body["parse_report"]["redacted_credentials"] == 3
    fixture = get(client, admin, project_id, "/fixtures")["items"][0]
    assert fixture["request"]["headers"] == {"X-API-Key": "[REDACTED]",
                                             "Authorization": "[REDACTED]"}
    assert fixture["request"]["query"] == {"api_key": "[REDACTED]", "page": 1}

    chunks: list[str] = []
    with database.engine.connect() as connection:
        for table in ("evidence_files", "fixtures", "endpoint_facts", "audit_records"):
            for row in connection.execute(text(f"SELECT * FROM {table}")):  # noqa: S608
                for value in row:
                    if isinstance(value, (bytes, memoryview)):
                        chunks.append(bytes(value).decode("utf-8", "replace"))
                    else:
                        chunks.append(json.dumps(value, default=str))
    stored = "\n".join(chunks)
    for secret in secrets:
        assert secret not in stored
    assert "[REDACTED]" in stored


# ============================================================================= manual examples


def test_manual_example_becomes_a_fixture_with_provenance(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    upload_demo(client, admin, project_id, "legacy_spec")
    response = client.post(f"{base(project_id)}/evidence/manual", headers=admin, json={
        "name": "typed in by hand",
        "request": {"method": "GET", "path": "/v1/charges/P-1/events",
                    "headers": {"X-API-Key": "typed-secret-9"}},
        "response": {"status": 200, "body": {"data": []}}})
    assert response.status_code == 201
    assert response.json()["kind"] == "manual_example"
    assert "typed-secret-9" not in response.text
    events = endpoint_id(client, admin, project_id, "legacy", "GET", "/v1/charges/{id}/events")
    fixture = get(client, admin, project_id, "/fixtures")["items"][0]
    assert fixture["endpoint_id"] == events
    assert fixture["evidence_file_id"] == response.json()["id"]


def test_invalid_manual_example_is_rejected(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    response = client.post(f"{base(project_id)}/evidence/manual", headers=admin, json={
        "name": "x", "request": {"method": "GET"}, "response": {"status": 200}})
    assert response.status_code == 400


# ============================================================================= versions, removal


def test_evidence_version_and_fingerprint_change_on_import_and_removal(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    empty = get(client, admin, project_id, "/evidence")
    assert empty["evidence_version"] == 0
    first = upload_demo(client, admin, project_id, "legacy_spec")
    after_one = get(client, admin, project_id, "/evidence")
    upload_demo(client, admin, project_id, "legacy_fixtures")
    after_two = get(client, admin, project_id, "/evidence")
    assert [after_one["evidence_version"], after_two["evidence_version"]] == [1, 2]
    assert len({empty["evidence_fingerprint"], after_one["evidence_fingerprint"],
                after_two["evidence_fingerprint"]}) == 3

    assert client.delete(f"{base(project_id)}/evidence/{first['id']}",
                         headers=admin).status_code == 204
    after_removal = get(client, admin, project_id, "/evidence")
    assert after_removal["evidence_version"] == 3
    assert after_removal["evidence_fingerprint"] != after_two["evidence_fingerprint"]
    # Same set of files as a hypothetical "fixtures only" project: same fingerprint
    # whatever the history was.
    upload_demo(client, admin, project_id, "legacy_spec")
    restored = get(client, admin, project_id, "/evidence")
    assert restored["evidence_version"] == 4
    assert restored["evidence_fingerprint"] == after_two["evidence_fingerprint"]


def test_removed_evidence_leaves_the_current_set_but_is_retained(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict],
    database: Database,
) -> None:
    spec_id = demo["legacy_spec"]["id"]
    assert client.delete(f"{base(project_id)}/evidence/{spec_id}", headers=admin).status_code == 204

    listing = get(client, admin, project_id, "/evidence")
    assert spec_id not in {item["id"] for item in listing["items"]} and listing["total"] == 4
    assert client.get(f"{base(project_id)}/evidence/{spec_id}", headers=admin).status_code == 404
    assert client.delete(f"{base(project_id)}/evidence/{spec_id}",
                         headers=admin).status_code == 404

    legacy = get(client, admin, project_id, "/endpoints?side=legacy")["items"]
    assert legacy and all(not item["from_specification"] for item in legacy)
    codes = {i["code"] for i in get(client, admin, project_id, "/evidence/missing")["items"]}
    assert "no_specification" in codes and "endpoint_without_specification" in codes

    # Retained for history: the row, its content and its declared facts still exist.
    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT deleted_at, length(content) FROM evidence_files WHERE id = :id"),
            {"id": spec_id}).one()
        facts = connection.execute(
            text("SELECT count(*) FROM endpoint_facts WHERE evidence_file_id = :id"),
            {"id": spec_id}).scalar()
    assert row[0] is not None and row[1] > 1000 and facts > 100


def test_evidence_import_and_removal_are_audited(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    record = upload_demo(client, admin, project_id, "legacy_spec")
    client.delete(f"{base(project_id)}/evidence/{record['id']}", headers=admin)
    activity = get(client, admin, project_id, "/activity")["items"]
    assert [item["action"] for item in activity] == ["evidence.remove", "evidence.import"]
    assert activity[1]["metadata"] == {
        "filename": "openapi.yaml", "side": "legacy", "kind": "openapi",
        "parse_status": "parsed", "evidence_version": 1}


def test_evidence_of_one_project_is_invisible_to_another(
    client: TestClient, admin: dict[str, str], project_id: str, demo: dict[str, dict]
) -> None:
    other = client.post("/api/v1/projects", headers=admin, json={
        "name": "Other", "legacy_name": "A", "replacement_name": "B"}).json()["id"]
    assert get(client, admin, other, "/evidence")["total"] == 0
    assert get(client, admin, other, "/endpoints")["total"] == 0
    assert get(client, admin, other, "/fixtures")["total"] == 0
    spec_id = demo["legacy_spec"]["id"]
    assert client.get(f"{base(other)}/evidence/{spec_id}", headers=admin).status_code == 404
    assert client.delete(f"{base(other)}/evidence/{spec_id}", headers=admin).status_code == 404
    legacy_endpoint = get(client, admin, project_id, "/endpoints")["items"][0]["id"]
    assert client.get(f"{base(other)}/endpoints/{legacy_endpoint}/facts",
                      headers=admin).status_code == 404
