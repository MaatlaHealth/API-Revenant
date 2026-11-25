"""Adapter compilation and dry run on the demonstration evidence (requirements 4.4, 5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from revenant.adapter.compiler import find_route, strip_credentials
from revenant.adapter.validator import validate
from revenant.db.session import Database
from revenant.transform.engine import match_path, transform_response
from tests.integration.test_evidence_api import base, get, upload
from tests.integration.test_reconstruction_api import (
    EXTRA_FIXTURE,
    by_key,
    every,
    import_demo,
    post,
    put,
    reconstruct,
)

DEMO = Path(__file__).resolve().parents[3] / "demo"
LEGACY_KEY = "gp_live_legacy_client_key_0001"
BASE_URL = "https://api.novapay.example"
# Inferred mappings a reviewer has to approve one by one for the demonstration.
REVIEWED = [
    "pagination:GET /v1/charges",
    "field:POST /v1/charges:response.body:$.status",
    "field:GET /v1/charges/{id}:response.body:$.status",
    "field:POST /v1/charges/{id}/refund:response.body:$.status",
]


def fixtures(name: str) -> list[dict]:
    return json.loads((DEMO / name).read_text("utf-8"))["fixtures"]


def approve(client: TestClient, admin: dict[str, str], project_id: str,
            keys: list[str]) -> dict[str, dict]:
    mappings = by_key(every(client, admin, project_id, "/mappings"))
    post(client, admin, project_id, "/mappings/bulk-approve",
         {"filter": {"confidence": "observed"}})
    for key in keys:
        post(client, admin, project_id, f"/mappings/{mappings[key]['id']}/approve")
    return mappings


def compile_adapter(client: TestClient, admin: dict[str, str], project_id: str,
                    expect: int = 201) -> dict:
    response = client.post(f"{base(project_id)}/adapters", headers=admin)
    assert response.status_code == expect, response.text
    return response.json()


def issues(body: dict) -> str:
    return " | ".join(item["issue"] for item in body["error"]["details"])


@pytest.fixture
def reviewed(client: TestClient, admin: dict[str, str], project_id: str) -> dict[str, dict]:
    """Demonstration evidence, reconstructed, with a replacement URL and the review done."""
    import_demo(client, admin, project_id)
    client.app.state.resolver = _public_resolver  # type: ignore[attr-defined]
    response = client.patch(base(project_id), headers=admin,
                            json={"replacement_base_url": BASE_URL})
    assert response.status_code == 200, response.text
    reconstruct(client, admin, project_id)
    return approve(client, admin, project_id, REVIEWED)


async def _public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def adapter(client: TestClient, admin: dict[str, str], project_id: str,
            reviewed: dict[str, dict]) -> dict:
    return compile_adapter(client, admin, project_id)


def run(client: TestClient, admin: dict[str, str], project_id: str, version: int,
        request: dict[str, Any]) -> dict:
    return post(client, admin, project_id, f"/adapters/{version}/dry-run", request)


# ============================================================================= compiling


def test_compiling_needs_a_current_reconstruction_and_approved_mappings(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    assert compile_adapter(client, admin, project_id, expect=409)["error"]["code"] == "conflict"
    import_demo(client, admin, project_id)
    reconstruct(client, admin, project_id)
    # Nothing is approved: there is nothing an adapter may contain.
    body = compile_adapter(client, admin, project_id, expect=422)
    assert body["error"]["code"] == "adapter_invalid"
    assert "no approved authentication mapping" in issues(body)
    assert "the project has no replacement base URL" in issues(body)
    assert get(client, admin, project_id, "/adapters")["total"] == 0

    upload(client, admin, project_id, "extra.json", EXTRA_FIXTURE, "legacy")
    stale = compile_adapter(client, admin, project_id, expect=409)
    assert stale["error"]["code"] == "stale_reconstruction"


def test_the_demonstration_compiles_into_a_valid_document(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict,
    reviewed: dict[str, dict]
) -> None:
    assert adapter["version"] == 1 and adapter["routes"] == 5
    document = adapter["document"]
    assert validate(document)[1] == []
    assert document["schema_version"] == "1.0" and document["project_slug"] == "gravepay"
    assert document["upstream"] == {"base_url": BASE_URL}
    assert document["auth"]["inbound"] == {"type": "api_key_header", "header": "X-API-Key"}
    assert document["auth"]["outbound"]["secret_ref"] == "replacement_credential"
    routes = {route["id"]: route for route in document["routes"]}
    assert set(routes) == {"get-v1-charges", "post-v1-charges", "get-v1-charges-id",
                           "get-v1-charges-id-receipt", "post-v1-charges-id-refund"}
    # The events endpoint pair is only inferred and was not approved: no route for it.
    assert adapter["excluded_routes"] == []
    assert "replacement_credential" in " ".join(adapter["warnings"])

    create = routes["post-v1-charges"]
    assert create["match"] == {"method": "POST", "path": "/v1/charges"}
    assert create["upstream"] == {"method": "POST", "path": "/payments"}
    assert {rule["to"] for rule in create["request"]["body"]["rules"]} == {
        "$.customer_id", "$.external_ref", "$.transaction.currency_code", "$.transaction.value"}
    assert [(e["when"]["status_in"], e["status"]) for e in create["response"]["errors"]] == [
        ([422], 400)]
    listing = routes["get-v1-charges"]
    loops = [rule for rule in listing["response"]["body"]["rules"] if rule["op"] == "each"]
    assert len(loops) == 1 and (loops[0]["from"], loops[0]["to"]) == ("body:$.items", "$.data")
    assert len(loops[0]["rules"]) == 6
    assert listing["pagination"]["strategy"] == "computed_cursor"
    assert routes["get-v1-charges-id"]["request"]["path"] == [
        {"op": "move", "from": "path:$.id", "to": "$.payment_id", "required": True,
         "mapping_id": reviewed["endpoint:GET /v1/charges/{id}"]["id"]}]

    project = get(client, admin, project_id, "")
    assert project["status"] == "adapter_ready"
    steps = {step["key"]: step for step in project["workflow"]}
    assert steps["adapter"]["complete"] is True
    assert steps["verification"]["blocked_by"] == "Not available in this build yet."
    assert get(client, admin, project_id, "/activity")["items"][0]["action"] == "adapter.compile"


def test_only_approved_mappings_reach_the_document(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    current = every(client, admin, project_id, "/mappings")
    status = {item["id"]: item["status"] for item in current}
    used = set(adapter["mapping_ids"])
    assert used and all(status[mapping_id] == "approved" for mapping_id in used)
    blob = json.dumps(adapter["document"])
    for item in current:
        if item["status"] != "approved":
            assert item["id"] not in blob, item["key"]
    # Every rule names the mapping that justifies it.
    found: list[str | None] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "op" in node or "when" in node and "status" in node:
                found.append(node.get("mapping_id"))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(adapter["document"])
    assert len(found) >= 40 and all(mapping_id in used for mapping_id in found)


def test_a_rejected_mapping_is_left_out_of_the_next_version(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict,
    reviewed: dict[str, dict]
) -> None:
    optional = reviewed["field:POST /v1/charges:request.body:$.external_ref"]["id"]
    post(client, admin, project_id, f"/mappings/{optional}/reject")
    second = compile_adapter(client, admin, project_id)
    assert second["version"] == 2 and optional not in json.dumps(second["document"])
    assert second["document_sha256"] != adapter["document_sha256"]
    # The first version is unchanged.
    first = get(client, admin, project_id, "/adapters/1")
    assert first["document"] == adapter["document"]
    assert first["document_sha256"] == adapter["document_sha256"]
    listed = get(client, admin, project_id, "/adapters")
    assert [item["version"] for item in listed["items"]] == [2, 1]
    assert "document" not in listed["items"][0]
    assert client.get(f"{base(project_id)}/adapters/9", headers=admin).status_code == 404


def test_compiling_the_same_mappings_twice_gives_the_same_document(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    again = compile_adapter(client, admin, project_id)
    assert again["version"] == 2
    assert again["document_sha256"] == adapter["document_sha256"]


def test_a_route_missing_required_fields_is_left_out_and_reported(
    client: TestClient, admin: dict[str, str], project_id: str, reviewed: dict[str, dict]
) -> None:
    needed = reviewed["field:POST /v1/charges:request.body:$.transaction.value"]["id"]
    post(client, admin, project_id, f"/mappings/{needed}/reject")
    lost = reviewed["field:GET /v1/charges/{id}:response.body:$.created"]["id"]
    post(client, admin, project_id, f"/mappings/{lost}/reject")
    # The inferred events route, once approved, still has nothing to build its response from.
    events = reviewed["endpoint:GET /v1/charges/{id}/events"]["id"]
    post(client, admin, project_id, f"/mappings/{events}/approve")
    result = compile_adapter(client, admin, project_id)
    excluded = {item["legacy"]: item["reasons"] for item in result["excluded_routes"]}
    assert excluded == {
        "POST /v1/charges": [
            "required replacement request fields are not produced: $.transaction.value"],
        "GET /v1/charges/{id}": ["required legacy response fields are not produced: $.created"],
        "GET /v1/charges/{id}/events": [
            "required legacy response fields are not produced: $.data[].at, $.data[].type"],
    }
    assert result["routes"] == 3
    assert {route["id"] for route in result["document"]["routes"]} == {
        "get-v1-charges", "get-v1-charges-id-receipt", "post-v1-charges-id-refund"}


def test_pagination_must_be_approved_for_a_paginated_route(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    import_demo(client, admin, project_id)
    client.app.state.resolver = _public_resolver  # type: ignore[attr-defined]
    client.patch(base(project_id), headers=admin, json={"replacement_base_url": BASE_URL})
    reconstruct(client, admin, project_id)
    approve(client, admin, project_id, REVIEWED[1:])
    result = compile_adapter(client, admin, project_id)
    reasons = {item["legacy"]: item["reasons"] for item in result["excluded_routes"]}
    assert "the legacy operation is paginated and no pagination mapping is approved" in reasons[
        "GET /v1/charges"]


def test_effects_are_compiled_from_approved_declarations_only(
    client: TestClient, admin: dict[str, str], project_id: str, reviewed: dict[str, dict]
) -> None:
    operations = {f"{o['legacy']['method']} {o['legacy']['path']}": o["id"]
                  for o in get(client, admin, project_id, "/operations")["items"]}
    put(client, admin, project_id, f"/operations/{operations['GET /v1/charges/{id}']}/effect",
        {"effect": "read_only", "note": "Reads one payment."})
    # An unknown proposal that nobody approved must not become a declaration.
    unknown = reviewed["effect:GET /v1/charges"]
    assert unknown["confidence"] == "inferred"
    document = compile_adapter(client, admin, project_id)["document"]
    effects = {route["id"]: route["effect"] for route in document["routes"]}
    assert effects == {
        "get-v1-charges": "unknown", "post-v1-charges": "state_changing",
        "get-v1-charges-id": "read_only",
        # A GET that issues a receipt on the replacement: declared in the specification.
        "get-v1-charges-id-receipt": "state_changing",
        "post-v1-charges-id-refund": "state_changing"}
    declared = next(r for r in document["routes"] if r["id"] == "get-v1-charges-id")
    assert declared["effect_mapping_id"] in json.dumps(
        every(client, admin, project_id, "/mappings?category=effect"))


def test_the_document_and_api_never_contain_a_credential_value(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict,
    database: Database
) -> None:
    secret = "nv_live_replacement_secret_9f8e7d"
    assert client.put(f"{base(project_id)}/credentials/outbound/replacement_credential",
                      headers=admin, json={"value": secret}).status_code in (200, 201)
    second = compile_adapter(client, admin, project_id)
    assert not [w for w in second["warnings"] if "replacement_credential" in w]
    result = run(client, admin, project_id, 2, {
        "method": "GET", "path": "/v1/charges/P-1002", "headers": {"X-API-Key": LEGACY_KEY}})
    for body in (second, result, get(client, admin, project_id, "/adapters/2")):
        assert secret not in json.dumps(body) and LEGACY_KEY not in json.dumps(body)
    with database.session() as session:
        stored = session.execute(text("SELECT document FROM adapter_versions")).all()
    # Row reprs are abbreviated, so the column values themselves are searched.
    whole = " ".join(str(row[0]) for row in stored)
    assert len(stored) == 2 and len(whole) > 10_000
    assert secret not in whole and LEGACY_KEY not in whole
    assert "secret_ref" in whole


def test_a_document_that_fails_static_validation_is_not_stored(
    client: TestClient, admin: dict[str, str], project_id: str, reviewed: dict[str, dict]
) -> None:
    """An unauthenticated runtime is a valid mapping shape, but the project has not allowed it."""
    auth = reviewed["auth"]
    edited = put(client, admin, project_id, f"/mappings/{auth['id']}", {"transformation": {
        "inbound": {"type": "none"},
        "outbound": {"type": "bearer", "secret_ref": "replacement_credential"}}})
    post(client, admin, project_id, f"/mappings/{edited['id']}/approve")
    body = compile_adapter(client, admin, project_id, expect=422)
    assert body["error"]["code"] == "adapter_invalid"
    assert "allow_unauthenticated_runtime" in issues(body)
    assert get(client, admin, project_id, "/adapters")["total"] == 0
    assert get(client, admin, project_id, "")["status"] == "reconstructed"


def test_credential_carriers_are_removed_before_any_rule_runs(adapter: dict) -> None:
    document, _ = validate(adapter["document"])
    assert document is not None
    headers, query, presented = strip_credentials(
        document, {"X-Api-Key": LEGACY_KEY, "Authorization": "Bearer t0ken", "Cookie": "c=1",
                   "Proxy-Authorization": "Basic abc", "X-Request-Id": "r-1"},
        {"page": "2"})
    assert headers == {"X-Request-Id": "r-1"} and query == {"page": "2"}
    assert presented == [LEGACY_KEY]


# ============================================================================= dry run


def test_every_recorded_request_transforms_into_the_recorded_replacement_request(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    recordings = {item["pair_with"]: item for item in fixtures("novapay/recordings.json")}
    checked = 0
    for fixture in fixtures("gravepay/fixtures.json"):
        request = fixture["request"]
        result = run(client, admin, project_id, 1, {
            "method": request["method"], "path": request["path"],
            "query": request.get("query", {}),
            "headers": {**request.get("headers", {}), "X-API-Key": LEGACY_KEY},
            "body": request.get("body")})
        assert result["sent"] is False and result["outcome"] == "ok", fixture["name"]
        expected = recordings[fixture["name"]]["request"]
        upstream = result["upstream"]
        assert (upstream["method"], upstream["path"]) == (expected["method"], expected["path"])
        assert upstream["url"] == BASE_URL + expected["path"]
        assert upstream["body"] == expected.get("body"), fixture["name"]
        assert upstream["query"] == expected.get("query", {}), fixture["name"]
        # The upstream request starts with no headers; the credential is not attached here.
        assert upstream["headers"] == {}
        assert LEGACY_KEY not in json.dumps(result)
        assert result["auth"]["inbound"]["credential"] == "present"
        assert result["auth"]["outbound"] == {"type": "bearer",
                                              "secret_ref": "replacement_credential",
                                              "attached": False}
        assert result["unmapped_fields"] == []
        checked += 1
    assert checked == 10


def test_every_recorded_replacement_response_transforms_into_the_legacy_response(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    """The response side of the compiled document, run through the engine directly."""
    document, problems = validate(adapter["document"])
    assert document is not None and problems == []
    recordings = {item["pair_with"]: item for item in fixtures("novapay/recordings.json")}
    for fixture in fixtures("gravepay/fixtures.json"):
        request, expected = fixture["request"], fixture["response"]
        route = find_route(document, request["method"], request["path"])
        assert route is not None, fixture["name"]
        recorded = recordings[fixture["name"]]["response"]
        result = transform_response(
            route, document.error_envelope.rules, recorded["status"], recorded["body"],
            {"query": request.get("query", {}), "body": request.get("body"),
             "path": match_path(route.match.path, request["path"])})
        assert result.status == expected["status"], fixture["name"]
        # Exactly the recorded legacy body: nothing missing, nothing extra.
        assert result.body == expected["body"], fixture["name"]
        assert result.unmapped_error is False and result.unmapped_status is False


def test_dry_run_reports_what_it_cannot_do(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    missing = run(client, admin, project_id, 1, {"method": "DELETE", "path": "/v1/charges/1"})
    assert (missing["matched"], missing["outcome"], missing["legacy_status"]) == (
        False, "no_route", 404)
    assert missing["auth"]["inbound"]["credential"] == "absent"
    events = run(client, admin, project_id, 1, {"method": "GET",
                                                "path": "/v1/charges/P-1/events"})
    assert events["outcome"] == "no_route"

    incomplete = run(client, admin, project_id, 1, {
        "method": "POST", "path": "/v1/charges", "body": {"customer": "C1", "currency": "ZAR"}})
    assert incomplete["outcome"] == "transform_error" and incomplete["legacy_status"] == 400
    assert incomplete["error"]["code"] == "missing_required"
    assert "upstream" not in incomplete and incomplete["sent"] is False

    extra = run(client, admin, project_id, 1, {
        "method": "POST", "path": "/v1/charges",
        "body": {"customer": "C1", "currency": "ZAR", "amount": 5, "metadata": {"a": 1}}})
    assert extra["unmapped_fields"] == ["body:$.metadata.a"]
    assert "metadata" not in json.dumps(extra["upstream"])

    for query in ({"page": 0}, {"page": "x"}, {"limit": 101}, {"limit": 0}):
        bad = run(client, admin, project_id, 1, {"method": "GET", "path": "/v1/charges",
                                                 "query": query})
        assert bad["error"]["code"] == "invalid_pagination", query
    default = run(client, admin, project_id, 1, {"method": "GET", "path": "/v1/charges"})
    assert default["upstream"]["query"] == {"cursor": "eyJwYWdlIjoxfQ==", "page_size": 20}


def test_path_values_cannot_change_the_upstream_path(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    for value, encoded in (("a b", "a%20b"), ("x?admin=1", "x%3Fadmin%3D1"),
                           ("..", ".."), ("a%2Fb", "a%252Fb"), ("id#frag", "id%23frag")):
        response = client.post(f"{base(project_id)}/adapters/1/dry-run", headers=admin, json={
            "method": "GET", "path": f"/v1/charges/{value}" if "?" not in value
            and "#" not in value else "/v1/charges/placeholder"})
        if "?" in value or "#" in value:
            continue  # such a path is not accepted by the endpoint at all
        assert response.json()["upstream"]["path"] == f"/payments/{encoded}"
    rejected = client.post(f"{base(project_id)}/adapters/1/dry-run", headers=admin, json={
        "method": "GET", "path": "/v1/charges/x?admin=1"})
    assert rejected.status_code == 400


def test_credential_carriers_never_reach_the_rules_or_the_output(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    result = run(client, admin, project_id, 1, {
        "method": "POST", "path": "/v1/charges",
        "headers": {"x-api-key": LEGACY_KEY, "Authorization": "Bearer other-secret-token",
                    "Cookie": "session=abc123secret"},
        "body": {"customer": "C1", "currency": "ZAR", "amount": 5}})
    blob = json.dumps(result)
    assert result["outcome"] == "ok"
    for secret in (LEGACY_KEY, "other-secret-token", "abc123secret"):
        assert secret not in blob
    # A client that puts its key in a body field would send it upstream: blocked.
    leaked = run(client, admin, project_id, 1, {
        "method": "POST", "path": "/v1/charges", "headers": {"X-API-Key": LEGACY_KEY},
        "body": {"customer": LEGACY_KEY, "currency": "ZAR", "amount": 5}})
    assert leaked["outcome"] == "credential_leak_blocked" and "upstream" not in leaked
    assert LEGACY_KEY not in json.dumps(leaked)


ROUTES = [("post", "/adapters"), ("get", "/adapters"), ("get", "/adapters/1"),
          ("post", "/adapters/1/dry-run")]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_adapter_routes_require_the_admin_token(
    client: TestClient, project_id: str, method: str, path: str
) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.request(method, f"{base(project_id)}{path}", headers=headers,
                              json={}).status_code == 401


def test_adapters_belong_to_their_project(
    client: TestClient, admin: dict[str, str], project_id: str, adapter: dict
) -> None:
    other = client.post("/api/v1/projects", headers=admin, json={
        "name": "Other", "legacy_name": "A", "replacement_name": "B"}).json()["id"]
    assert client.get(f"{base(other)}/adapters/1", headers=admin).status_code == 404
    assert client.post(f"{base(other)}/adapters/1/dry-run", headers=admin,
                       json={"method": "GET", "path": "/v1/charges"}).status_code == 404
    assert get(client, admin, other, "/adapters")["total"] == 0
