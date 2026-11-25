"""Reconstruction and mapping review on the demonstration evidence
(requirements 3 and 4, and 8.4, 8.11, 8.14, 8.20, 8.35)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from revenant.db.session import Database
from revenant.reconstruction.shapes import validate_transformation
from tests.integration.test_evidence_api import FILES, base, get, upload, upload_demo

ORDER = ["legacy_spec", "legacy_fixtures", "legacy_notes", "replacement_spec", "recordings"]
EXTRA_FIXTURE = json.dumps({"revenant_fixtures": "1.0", "fixtures": [{
    "name": "ping",
    "request": {"method": "GET", "path": "/v1/ping", "headers": {}},
    "response": {"status": 200, "headers": {}, "body": {"ok": "yes"}},
}]}).encode()


def post(client: TestClient, admin: dict[str, str], project_id: str, path: str,
         body: Any = None, expect: int = 200) -> dict:
    response = client.post(f"{base(project_id)}{path}", headers=admin, json=body)
    assert response.status_code == expect, response.text
    return response.json()


def put(client: TestClient, admin: dict[str, str], project_id: str, path: str, body: Any,
        expect: int = 200) -> dict:
    response = client.put(f"{base(project_id)}{path}", headers=admin, json=body)
    assert response.status_code == expect, response.text
    return response.json()


def patch(client: TestClient, admin: dict[str, str], project_id: str, path: str, body: Any,
          expect: int = 200) -> dict:
    response = client.patch(f"{base(project_id)}{path}", headers=admin, json=body)
    assert response.status_code == expect, response.text
    return response.json()


def import_demo(client: TestClient, admin: dict[str, str], project_id: str,
                order: list[str] = ORDER) -> None:
    for key in order:
        upload_demo(client, admin, project_id, key)


def reconstruct(client: TestClient, admin: dict[str, str], project_id: str) -> dict:
    return post(client, admin, project_id, "/reconstructions", expect=201)


def every(client: TestClient, admin: dict[str, str], project_id: str, path: str) -> list[dict]:
    joiner = "&" if "?" in path else "?"
    body = get(client, admin, project_id, f"{path}{joiner}limit=200")
    assert body["total"] == len(body["items"])
    return body["items"]


def by_key(items: list[dict]) -> dict[str, dict]:
    assert len({item["key"] for item in items}) == len(items)
    return {item["key"]: item for item in items}


@pytest.fixture
def rebuilt(client: TestClient, admin: dict[str, str], project_id: str) -> dict:
    """The demonstration evidence, imported and reconstructed once."""
    import_demo(client, admin, project_id)
    return reconstruct(client, admin, project_id)


@pytest.fixture
def mappings(client: TestClient, admin: dict[str, str], project_id: str,
             rebuilt: dict) -> dict[str, dict]:
    return by_key(every(client, admin, project_id, "/mappings"))


def projection(client: TestClient, admin: dict[str, str], project_id: str) -> str:
    """Everything the engine produced, without row ids, as comparable text."""

    def refs(items: list[dict]) -> list[tuple]:
        return [(r["type"], r.get("filename"), r.get("locator"), r.get("name")) for r in items]

    out: dict[str, Any] = {
        "mappings": [(m["key"], m["category"], m["legacy_ref"], m["replacement_ref"],
                      m["transformation"], m["confidence"], m["rule_name"], m["rationale"],
                      m["requires_approval"], refs(m["evidence_refs"]))
                     for m in every(client, admin, project_id, "/mappings")],
        "gaps": [(g["key"], g["severity"], g["description"], g["behaviour_key"],
                  refs(g["evidence_refs"])) for g in every(client, admin, project_id, "/gaps")],
        "invariants": [(i["key"], i["kind"], i["definition"], i["confidence"], i["status"])
                       for i in every(client, admin, project_id, "/invariants")],
        "behaviours": [(b["key"], b["kind"], b["endpoint"], refs(b["evidence_refs"]))
                       for b in every(client, admin, project_id, "/behaviours")],
    }
    return json.dumps(out, sort_keys=True)


# ============================================================================= running


def test_reconstruction_needs_endpoints_on_both_sides(
    client: TestClient, admin: dict[str, str], project_id: str
) -> None:
    response = client.post(f"{base(project_id)}/reconstructions", headers=admin)
    assert response.status_code == 400
    issues = [item["issue"] for item in response.json()["error"]["details"]]
    assert issues == ["no legacy endpoints have been imported",
                      "no replacement endpoints have been imported"]
    upload_demo(client, admin, project_id, "legacy_spec")
    response = client.post(f"{base(project_id)}/reconstructions", headers=admin)
    assert [i["issue"] for i in response.json()["error"]["details"]] == [
        "no replacement endpoints have been imported"]
    assert client.get(f"{base(project_id)}/reconstructions/latest",
                      headers=admin).status_code == 404


def test_reconstruction_of_the_demonstration(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    assert rebuilt["sequence"] == 1 and rebuilt["stale"] is False
    assert rebuilt["evidence_version"] == 5 and rebuilt["evidence_files_not_analysed"] == 0
    summary = rebuilt["summary"]
    assert summary["endpoints"] == {"legacy": 6, "replacement": 6, "paired": 6,
                                    "unpaired_legacy": 0}
    assert (summary["mappings"]["total"], summary["mappings"]["observed"],
            summary["mappings"]["inferred"]) == (55, 47, 8)
    assert (summary["gaps"]["total"], summary["gaps"]["blocking"]) == (11, 1)
    assert summary["invariants"]["by_kind"] == {"echo": 10, "error": 3, "pagination": 1,
                                                "schema": 14}
    assert rebuilt["inventory_size"] == summary["behaviours"]["total"] == 27
    assert rebuilt["inventory_diff"] == {"previous_size": None, "size": 27, "added": [],
                                         "removed": []}
    pairs = {p["legacy"]: p for p in rebuilt["endpoint_pairs"]}
    assert pairs["GET /v1/charges/{id}"]["replacement"] == "GET /payments/{payment_id}"
    assert pairs["GET /v1/charges/{id}"]["confidence"] == "observed"
    # No recording links the events endpoints: the pair rests on similarity alone.
    assert pairs["GET /v1/charges/{id}/events"]["confidence"] == "inferred"
    assert set(rebuilt["differences"]) >= {"endpoint", "request_field", "response_field", "type",
                                           "enum", "auth", "pagination", "error", "status_code"}
    project = get(client, admin, project_id, "")
    assert project["status"] == "reconstructed"
    steps = {step["key"]: step for step in project["workflow"]}
    assert steps["reconstruction"]["complete"] is True
    assert steps["adapter"]["blocked_by"] == "Not available in this build yet."
    assert get(client, admin, project_id, "/reconstructions/latest")["id"] == rebuilt["id"]


def test_every_mapping_records_what_requirement_3_2_lists(mappings: dict[str, dict]) -> None:
    for item in mappings.values():
        assert item["legacy_ref"] and item["replacement_ref"] and item["rule_name"]
        assert item["rationale"] and isinstance(item["transformation"], dict)
        assert item["confidence"] in ("observed", "inferred")
        assert item["status"] == "proposed" and item["origin"] == "engine"
        # An inferred mapping always needs individual approval.
        assert item["requires_approval"] is (item["confidence"] == "inferred")


def test_observed_mappings_are_backed_by_evidence_that_exists(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    fixtures = {f["id"] for f in every(client, admin, project_id, "/fixtures")}
    files = {f["id"]: f["filename"] for f in every(client, admin, project_id, "/evidence")}
    locators: set[tuple[str, str]] = set()
    for endpoint in every(client, admin, project_id, "/endpoints"):
        for fact in get(client, admin, project_id,
                        f"/endpoints/{endpoint['id']}/facts")["items"]:
            locators.add((fact["evidence_file"]["id"], fact["locator"]))
    checked = 0
    for item in mappings.values():
        if item["confidence"] == "observed" and item["rule_name"] != "unsafe_method":
            assert item["evidence_refs"], item["key"]
        for ref in item["evidence_refs"]:
            assert files[ref["evidence_file_id"]] == ref["filename"]
            if ref["type"] == "fixture":
                assert ref["fixture_id"] in fixtures
            else:
                assert (ref["evidence_file_id"], ref["locator"]) in locators
            checked += 1
    assert checked > 150


def test_field_mappings_follow_the_recordings(mappings: dict[str, dict]) -> None:
    create = "field:POST /v1/charges:request.body:"
    assert mappings[create + "$.customer_id"]["transformation"] == {
        "section": "request.body",
        "rule": {"op": "move", "from": "body:$.customer", "to": "$.customer_id",
                 "required": True}}
    nested = mappings[create + "$.transaction.value"]
    assert nested["transformation"]["rule"]["from"] == "body:$.amount"
    assert nested["confidence"] == "observed"
    assert nested["rule_name"] == "fixture_value_correlation"
    assert mappings[create + "$.external_ref"]["transformation"]["rule"] == {
        "op": "move", "from": "body:$.reference", "to": "$.external_ref"}

    created = mappings["field:POST /v1/charges:response.body:$.created"]
    assert created["category"] == "type"
    assert created["transformation"]["rule"] == {
        "op": "convert", "from": "body:$.created_at", "to": "$.created",
        "as": "epoch_s_to_iso8601", "required": True}

    item = mappings["field:GET /v1/charges:response.body:$.data[].amount"]
    assert item["transformation"] == {
        "section": "response.body",
        "rule": {"op": "move", "from": "item:$.transaction.value", "to": "$.amount",
                 "required": True},
        "each": {"from": "body:$.items", "to": "$.data"}}


def test_enumerations_are_observed_only_where_every_value_was_recorded(
    mappings: dict[str, dict]
) -> None:
    wanted = {"COMPLETED": "succeeded", "DECLINED": "failed", "PROCESSING": "pending"}
    listed = mappings["field:GET /v1/charges:response.body:$.data[].status"]
    assert listed["category"] == "enum" and listed["confidence"] == "observed"
    assert listed["transformation"]["rule"]["map"] == wanted
    assert listed["transformation"]["rule"]["on_unmapped"] == "error"
    # Only COMPLETED was recorded for a refund; the rest is borrowed, so it is inferred.
    refund = mappings["field:POST /v1/charges/{id}/refund:response.body:$.status"]
    assert refund["transformation"]["rule"]["map"] == wanted
    assert refund["confidence"] == "inferred" and refund["requires_approval"] is True
    assert "observed on another endpoint" in refund["rationale"]


def test_routes_authentication_pagination_and_errors(mappings: dict[str, dict]) -> None:
    route = mappings["endpoint:GET /v1/charges/{id}"]
    assert route["transformation"] == {
        "match": {"method": "GET", "path": "/v1/charges/{id}"},
        "upstream": {"method": "GET", "path": "/payments/{payment_id}"},
        "path_params": [{"from": "path:$.id", "to": "payment_id"}]}
    events = mappings["endpoint:GET /v1/charges/{id}/events"]
    assert events["confidence"] == "inferred" and events["rule_name"] == "endpoint_similarity"

    auth = mappings["auth"]
    assert auth["transformation"] == {
        "inbound": {"type": "api_key_header", "header": "X-API-Key"},
        "outbound": {"type": "bearer", "secret_ref": "replacement_credential"}}

    paging = mappings["pagination:GET /v1/charges"]
    assert paging["transformation"] == {
        "strategy": "computed_cursor",
        "legacy": {"page_param": "page", "limit_param": "limit", "first_page": 1,
                   "default_limit": 20, "max_limit": 100},
        "upstream": {"cursor_param": "cursor", "size_param": "page_size"},
        "cursor": {"encoding": "base64_json", "shape": {"page": "$page"}},
        "response": {"items_from": "body:$.items", "items_to": "$.data",
                     "has_more_from_presence_of": "body:$.next_cursor",
                     "has_more_to": "$.has_more",
                     "echo": {"$.limit": "$limit", "$.page": "$page"}}}
    # has_more was only ever recorded as true, so the whole mapping needs approval.
    assert paging["confidence"] == "inferred"
    assert "only true was ever recorded" in paging["rationale"]

    error = mappings["error:POST /v1/charges:422:CURRENCY_NOT_SUPPORTED"]
    assert error["transformation"] == {
        "kind": "error_rule",
        "when": {"status_in": [422], "equals": ["body:$.code", "CURRENCY_NOT_SUPPORTED"]},
        "status": 400,
        "vars": {"code": {"const": "invalid_currency"}, "message": {"from": "body:$.detail"}}}
    assert mappings["error_envelope"]["transformation"]["rules"] == [
        {"op": "move", "from": "var:$.code", "to": "$.error.code"},
        {"op": "move", "from": "var:$.message", "to": "$.error.message"}]


def test_no_mapping_carries_a_credential_or_touches_a_credential_carrier(
    mappings: dict[str, dict]
) -> None:
    for item in mappings.values():
        blob = json.dumps(item["transformation"]).lower()
        if item["category"] != "auth":
            assert "x-api-key" not in blob and "authorization" not in blob, item["key"]
    assert "secret_ref" in json.dumps(mappings["auth"]["transformation"])
    assert not [key for key in mappings if "request.headers" in key]


def test_engine_proposals_are_valid_rules_of_the_language(mappings: dict[str, dict]) -> None:
    for item in mappings.values():
        if item["category"] != "effect":
            assert validate_transformation(item["category"], item["transformation"]) == [], \
                item["key"]


def test_what_is_not_established_is_a_gap_and_not_a_mapping(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    gaps = by_key(every(client, admin, project_id, "/gaps"))
    events = gaps["response_field:GET /v1/charges/{id}/events:unmapped"]
    assert events["severity"] == "blocking" and events["resolution"] == "open"
    assert "$.data[].at (required)" in events["description"]
    assert "$.data[].type (required)" in events["description"]
    assert not [key for key in mappings
                if key.startswith("field:GET /v1/charges/{id}/events:")]
    # Documented but never recorded: reported, and tied to the behaviour it concerns.
    undocumented = gaps["status_code:POST /v1/charges:429:no_example"]
    assert undocumented["behaviour_key"] == "POST /v1/charges → 429"
    assert gaps["pagination:GET /v1/charges:termination"]["behaviour_key"] == \
        "pagination termination: GET /v1/charges"
    assert sum(1 for gap in gaps.values() if gap["category"] == "status_code") == 7
    for gap in gaps.values():
        assert gap["missing"] and gap["title"] and gap["behaviour_key"]


# ============================================================================= determinism


def test_reconstruction_is_deterministic(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    first = projection(client, admin, project_id)
    again = reconstruct(client, admin, project_id)
    assert again["sequence"] == 2 and again["previous_reconstruction_id"] == rebuilt["id"]
    assert projection(client, admin, project_id) == first
    assert again["inventory_fingerprint"] == rebuilt["inventory_fingerprint"]
    assert again["inventory_diff"] == {"previous_size": 27, "size": 27, "added": [],
                                       "removed": []}
    assert again["summary"] == rebuilt["summary"]


def test_reconstruction_does_not_depend_on_import_order(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    first = projection(client, admin, project_id)
    other = client.post("/api/v1/projects", headers=admin, json={
        "name": "Second", "legacy_name": "GravePay", "replacement_name": "NovaPay"}).json()["id"]
    import_demo(client, admin, other, list(reversed(ORDER)))
    second = reconstruct(client, admin, other)
    assert projection(client, admin, other) == first
    assert second["inventory_fingerprint"] == rebuilt["inventory_fingerprint"]


# ============================================================================= versions


def test_reconstruction_becomes_stale_when_evidence_changes(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    added = upload(client, admin, project_id, "extra.json", EXTRA_FIXTURE, "legacy").json()
    latest = get(client, admin, project_id, "/reconstructions/latest")
    assert latest["stale"] is True and latest["evidence_files_not_analysed"] == 1
    assert latest["evidence_version"] == 5
    assert get(client, admin, project_id, "/mappings")["stale"] is True
    steps = {s["key"]: s for s in get(client, admin, project_id, "")["workflow"]}
    assert steps["reconstruction"]["complete"] is False
    assert steps["reconstruction"]["blocked_by"] == \
        "Evidence has changed since the last reconstruction. Run it again."
    # Staleness is a comparison of fingerprints: restoring the evidence set clears it.
    assert client.delete(f"{base(project_id)}/evidence/{added['id']}",
                         headers=admin).status_code == 204
    assert get(client, admin, project_id, "/reconstructions/latest")["stale"] is False


def test_staleness_is_not_stored(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict,
    database: Database
) -> None:
    with database.session() as session:
        columns = {row[1] if database.engine.dialect.name == "sqlite" else row[0]
                   for row in session.execute(text(
                       "PRAGMA table_info(reconstructions)"
                       if database.engine.dialect.name == "sqlite" else
                       "SELECT column_name FROM information_schema.columns "
                       "WHERE table_name = 'reconstructions'"))}
    assert "stale" not in columns and "evidence_fingerprint" in columns


def test_a_completed_inventory_never_changes_and_the_difference_is_reported(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    path = f"/reconstructions/{rebuilt['id']}/behaviours"
    before = every(client, admin, project_id, path)
    added = upload(client, admin, project_id, "extra.json", EXTRA_FIXTURE, "legacy").json()
    # New evidence does not enter the existing inventory.
    assert every(client, admin, project_id, path) == before
    assert get(client, admin, project_id, "/behaviours")["inventory_size"] == 27

    second = reconstruct(client, admin, project_id)
    assert second["inventory_diff"] == {
        "previous_size": 27, "size": 29, "removed": [],
        "added": ["GET /v1/ping → 200", "gap: endpoint:GET /v1/ping:unpaired"]}
    assert second["inventory_size"] == 29
    assert every(client, admin, project_id, path) == before
    assert get(client, admin, project_id, path)["inventory_fingerprint"] == \
        rebuilt["inventory_fingerprint"]

    client.delete(f"{base(project_id)}/evidence/{added['id']}", headers=admin)
    third = reconstruct(client, admin, project_id)
    assert third["inventory_diff"] == {
        "previous_size": 29, "size": 27, "added": [],
        "removed": ["GET /v1/ping → 200", "gap: endpoint:GET /v1/ping:unpaired"]}
    history = get(client, admin, project_id, "/reconstructions")
    assert [item["sequence"] for item in history["items"]] == [3, 2, 1]


def test_earlier_reconstructions_are_read_only(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict],
    rebuilt: dict
) -> None:
    old_mapping = mappings["auth"]["id"]
    old_behaviour = every(client, admin, project_id, "/behaviours")[0]["id"]
    old_gap = every(client, admin, project_id, "/gaps")[0]["id"]
    old_invariant = every(client, admin, project_id, "/invariants")[0]["id"]
    reconstruct(client, admin, project_id)
    for method, path, body in (
        ("post", f"/mappings/{old_mapping}/approve", None),
        ("post", f"/mappings/{old_mapping}/reject", None),
        ("put", f"/mappings/{old_mapping}", {"transformation": {}}),
        ("patch", f"/behaviours/{old_behaviour}", {"scope": "out_of_scope", "note": "n"}),
        ("patch", f"/gaps/{old_gap}", {"resolution": "accepted_risk", "note": "n"}),
        ("post", f"/invariants/{old_invariant}/reject", None),
    ):
        response = client.request(method, f"{base(project_id)}{path}", headers=admin, json=body)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["error"]["code"] == "immutable"
    assert get(client, admin, project_id, f"/mappings/{old_mapping}")["status"] == "proposed"


# ============================================================================= review


def test_approve_and_reject_are_recorded_with_actor_note_and_time(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    target = mappings["field:POST /v1/charges:request.body:$.customer_id"]["id"]
    approved = post(client, admin, project_id, f"/mappings/{target}/approve",
                    {"note": "checked against fixtures"})
    assert approved["status"] == "approved"
    rejected = post(client, admin, project_id, f"/mappings/{target}/reject", {"note": "wrong"})
    assert rejected["status"] == "rejected"
    decisions = get(client, admin, project_id, f"/mappings/{target}")["decisions"]
    assert [(d["decision"], d["actor"], d["note"]) for d in decisions] == [
        ("approved", "admin", "checked against fixtures"), ("rejected", "admin", "wrong")]
    assert all(d["created_at"] for d in decisions)
    actions = [a["action"] for a in get(client, admin, project_id, "/activity")["items"]]
    assert actions[:2] == ["mapping.reject", "mapping.approve"]
    assert len(every(client, admin, project_id, "/mappings?status=rejected")) == 1


def test_decisions_are_append_only(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict],
    database: Database
) -> None:
    target = mappings["auth"]["id"]
    for action in ("approve", "reject", "approve"):
        post(client, admin, project_id, f"/mappings/{target}/{action}")
    with database.session() as session:
        count = session.execute(text(
            "SELECT count(*) FROM mapping_decisions WHERE mapping_id = :id"),
            {"id": target}).scalar()
    assert count == 3


def test_an_edit_is_validated_and_supersedes_the_proposal(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    original = mappings["field:POST /v1/charges:request.body:$.external_ref"]
    edited = put(client, admin, project_id, f"/mappings/{original['id']}", {
        "transformation": {"section": "request.body",
                           "rule": {"op": "const", "to": "$.external_ref", "value": "legacy"}},
        "note": "always the same marker"})
    assert edited["origin"] == "human" and edited["supersedes_id"] == original["id"]
    assert edited["status"] == "proposed" and edited["confidence"] == "inferred"
    assert edited["requires_approval"] is True and edited["evidence_refs"] == []
    assert edited["key"] == original["key"]
    old = get(client, admin, project_id, f"/mappings/{original['id']}")
    assert old["status"] == "superseded"
    assert [d["decision"] for d in old["decisions"]] == ["edited"]
    # The superseded proposal leaves the working set but is kept for audit.
    current = by_key(every(client, admin, project_id, "/mappings"))
    assert current[original["key"]]["id"] == edited["id"] and len(current) == len(mappings)
    assert len(every(client, admin, project_id, "/mappings?status=superseded")) == 1
    for action in ("approve", "reject"):
        post(client, admin, project_id, f"/mappings/{original['id']}/{action}", expect=409)
    put(client, admin, project_id, f"/mappings/{original['id']}",
        {"transformation": edited["transformation"]}, expect=409)
    assert post(client, admin, project_id,
                f"/mappings/{edited['id']}/approve")["status"] == "approved"


BAD_RULES = [
    ({"op": "eval", "code": "__import__('os').system('id')"}, "op"),
    ({"op": "move", "from": "body:$.a", "to": "$.b", "script": "x"}, "script"),
    ({"op": "move", "from": "body:$.a", "to": "$.b[*]"}, "not a pointer"),
    ({"op": "move", "from": "$.a", "to": "$.b"}, "not a path"),
    ({"op": "move", "from": "env:$.HOME", "to": "$.b"}, "cannot be read here"),
    ({"op": "move", "from": "header:$.Authorization", "to": "$.b"}, "credential carrier"),
    ({"op": "convert", "from": "body:$.a", "to": "$.b", "as": "python"}, "unknown conversion"),
    ({"op": "template", "to": "$.b", "template": "{a.__class__}", "vars": {}}, "placeholder"),
]


@pytest.mark.parametrize(("rule", "expected"), BAD_RULES)
def test_invalid_edits_are_rejected_and_nothing_is_stored(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict],
    rule: dict, expected: str
) -> None:
    original = mappings["field:POST /v1/charges:request.body:$.external_ref"]
    response = client.put(f"{base(project_id)}/mappings/{original['id']}", headers=admin, json={
        "transformation": {"section": "request.body", "rule": rule}})
    assert response.status_code == 400, response.text
    issues = " ".join(d["issue"] for d in response.json()["error"]["details"])
    assert expected in issues
    assert get(client, admin, project_id, f"/mappings/{original['id']}")["status"] == "proposed"
    assert len(every(client, admin, project_id, "/mappings?status=superseded")) == 0


def test_edits_cannot_write_a_credential_header_or_change_the_legacy_route(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    field = mappings["field:POST /v1/charges:request.body:$.external_ref"]["id"]
    for header in ("Authorization", "cookie", "Proxy-Authorization"):
        response = client.put(f"{base(project_id)}/mappings/{field}", headers=admin, json={
            "transformation": {"section": "request.headers", "rule": {
                "op": "move", "from": "body:$.reference", "to": f"$.{header}"}}})
        assert response.status_code == 400
        assert "credential carrier" in response.text
    route = mappings["endpoint:GET /v1/charges/{id}"]
    changed = json.loads(json.dumps(route["transformation"]))
    changed["match"]["path"] = "/v1/other/{id}"
    assert "cannot be changed" in client.put(
        f"{base(project_id)}/mappings/{route['id']}", headers=admin,
        json={"transformation": changed}).text
    unknown = json.loads(json.dumps(route["transformation"]))
    unknown["upstream"]["path"] = "/internal/{payment_id}"
    assert "no such endpoint" in client.put(
        f"{base(project_id)}/mappings/{route['id']}", headers=admin,
        json={"transformation": unknown}).text
    missing = json.loads(json.dumps(route["transformation"]))
    missing["path_params"] = []
    assert "exactly one source is needed" in client.put(
        f"{base(project_id)}/mappings/{route['id']}", headers=admin,
        json={"transformation": missing}).text
    auth = mappings["auth"]
    leaked = {"inbound": auth["transformation"]["inbound"],
              "outbound": {"type": "bearer", "token": "example-token-value-123"}}
    response = client.put(f"{base(project_id)}/mappings/{auth['id']}", headers=admin,
                          json={"transformation": leaked})
    assert response.status_code == 400 and "example-token-value-123" not in response.text


def test_bulk_approval_by_filter_takes_observed_mappings_only(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    result = post(client, admin, project_id, "/mappings/bulk-approve",
                  {"filter": {"confidence": "observed"}, "note": "bulk"})
    assert result["approved_count"] == 47 and result["skipped"] == []
    after = by_key(every(client, admin, project_id, "/mappings"))
    for key, item in after.items():
        assert item["status"] == ("approved" if item["confidence"] == "observed"
                                  else "proposed"), key
    assert sum(1 for item in after.values() if item["status"] == "proposed") == 8
    one = get(client, admin, project_id, f"/mappings/{mappings['auth']['id']}")
    assert [(d["decision"], d["note"]) for d in one["decisions"]] == [("approved", "bulk")]
    # A second run finds nothing left to approve.
    assert post(client, admin, project_id, "/mappings/bulk-approve",
                {"filter": {"confidence": "observed"}})["approved_count"] == 0


@pytest.mark.parametrize("body", [
    {"filter": {"confidence": "inferred"}},
    {"filter": {}},
    {"filter": {"confidence": "observed"}, "ids": []},
    {},
    {"filter": {"confidence": "observed", "status": "proposed"}},
])
def test_inferred_mappings_cannot_be_selected_by_filter(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict],
    body: dict
) -> None:
    response = client.post(f"{base(project_id)}/mappings/bulk-approve", headers=admin, json=body)
    assert response.status_code == 400
    assert not every(client, admin, project_id, "/mappings?status=approved")


def test_bulk_approval_by_id_and_by_category(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    inferred = mappings["pagination:GET /v1/charges"]["id"]
    observed = mappings["auth"]["id"]
    post(client, admin, project_id, f"/mappings/{observed}/approve")
    result = post(client, admin, project_id, "/mappings/bulk-approve",
                  {"ids": [inferred, observed]})
    assert result["approved"] == [inferred]
    assert result["skipped"] == [{"id": observed, "reason": "status is approved"}]
    by_category = post(client, admin, project_id, "/mappings/bulk-approve",
                       {"filter": {"confidence": "observed", "category": "status_code"}})
    assert by_category["approved_count"] == 6
    post(client, admin, project_id, "/mappings/bulk-approve", {"ids": ["nope"]}, expect=404)


# ============================================================================= effects


def operations(client: TestClient, admin: dict[str, str], project_id: str) -> dict[str, dict]:
    items = get(client, admin, project_id, "/operations")["items"]
    return {f"{i['legacy']['method']} {i['legacy']['path']}": i for i in items}


def test_the_engine_never_proposes_read_only(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    effects = {key: item for key, item in mappings.items() if item["category"] == "effect"}
    assert len(effects) == 6
    assert {item["transformation"]["effect"] for item in effects.values()} == {
        "state_changing", "unknown"}
    assert effects["effect:POST /v1/charges"]["rule_name"] == "unsafe_method"
    # A GET whose replacement issues a receipt: marked in the specification.
    receipt = effects["effect:GET /v1/charges/{id}/receipt"]
    assert receipt["transformation"] == {"effect": "state_changing"}
    assert receipt["rule_name"] == "evidence_marker" and receipt["evidence_refs"]
    # Safe methods alone establish nothing.
    plain = effects["effect:GET /v1/charges/{id}"]
    assert plain["transformation"] == {"effect": "unknown"}
    assert plain["confidence"] == "inferred"

    listed = operations(client, admin, project_id)
    assert len(listed) == 6
    assert {name: op["effective_effect"] for name, op in listed.items()} == {
        "GET /v1/charges": "unknown", "POST /v1/charges": "state_changing",
        "GET /v1/charges/{id}": "unknown", "GET /v1/charges/{id}/events": "unknown",
        "GET /v1/charges/{id}/receipt": "unknown",
        "POST /v1/charges/{id}/refund": "state_changing"}
    assert all(op["declared_effect"] is None for op in listed.values())
    assert listed["GET /v1/charges/{id}/receipt"]["proposed_effect"] == "state_changing"
    assert listed["GET /v1/charges/{id}/receipt"]["evidence_markers"] == [
        {"endpoint": "GET /payments/{payment_id}/receipt", "effect": "state_changing"}]


def test_read_only_needs_an_explicit_declaration_with_a_note(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    listed = operations(client, admin, project_id)
    target = listed["GET /v1/charges/{id}"]["id"]
    for body in ({"effect": "read_only"}, {"effect": "read_only", "note": ""},
                 {"effect": "read_only", "note": "   "}, {"effect": "harmless", "note": "x"}):
        put(client, admin, project_id, f"/operations/{target}/effect", body, expect=400)
    assert operations(client, admin, project_id)["GET /v1/charges/{id}"][
        "effective_effect"] == "unknown"

    declared = put(client, admin, project_id, f"/operations/{target}/effect",
                   {"effect": "read_only", "note": "Reads one payment; confirmed with NovaPay."})
    assert declared["declared_effect"] == declared["effective_effect"] == "read_only"
    assert declared["declared_by"] == "admin" and declared["declared_at"]
    assert declared["note"] == "Reads one payment; confirmed with NovaPay."
    assert declared["origin"] == "human"
    stored = get(client, admin, project_id, f"/mappings/{declared['effect_mapping_id']}")
    assert stored["status"] == "approved" and stored["transformation"] == {"effect": "read_only"}
    assert [(d["decision"], d["actor"]) for d in stored["decisions"]] == [("approved", "admin")]
    assert get(client, admin, project_id,
               f"/mappings/{mappings['effect:GET /v1/charges/{id}']['id']}")[
        "status"] == "superseded"
    record = get(client, admin, project_id, "/activity")["items"][0]
    assert record["action"] == "operation.effect.declare"
    assert record["metadata"]["effect"] == "read_only" and record["metadata"]["note"]

    # A declaration can always be made stricter again.
    stricter = put(client, admin, project_id, f"/operations/{target}/effect",
                   {"effect": "state_changing", "note": "It turned out to write an audit row."})
    assert stricter["effective_effect"] == "state_changing"
    effects = [m for m in every(client, admin, project_id, "/mappings?category=effect")
               if m["legacy_endpoint_id"] == target]
    assert len(effects) == 1


def test_read_only_is_rejected_on_unsafe_methods_and_against_evidence(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    listed = operations(client, admin, project_id)
    note = "I am sure."
    for name, reason in (
        ("POST /v1/charges", "method POST is not GET, HEAD or OPTIONS"),
        ("POST /v1/charges/{id}/refund", "method POST is not GET, HEAD or OPTIONS"),
        ("GET /v1/charges/{id}/receipt", "marks GET /payments/{payment_id}/receipt as "
                                         "state-changing"),
    ):
        response = client.put(
            f"{base(project_id)}/operations/{listed[name]['id']}/effect", headers=admin,
            json={"effect": "read_only", "note": note})
        assert response.status_code == 400, name
        assert reason in response.text
    after = operations(client, admin, project_id)
    assert all(op["declared_effect"] is None for op in after.values())
    # The method floor holds whatever is declared.
    post_op = listed["POST /v1/charges"]["id"]
    for effect in ("unknown", "state_changing"):
        assert put(client, admin, project_id, f"/operations/{post_op}/effect",
                   {"effect": effect, "note": note})["effective_effect"] == "state_changing"
    # Declaring a GET state-changing is always accepted.
    get_op = listed["GET /v1/charges"]["id"]
    assert put(client, admin, project_id, f"/operations/{get_op}/effect",
               {"effect": "state_changing", "note": note})["declared_effect"] == "state_changing"
    put(client, admin, project_id, "/operations/unknown-id/effect",
        {"effect": "unknown", "note": note}, expect=404)


def test_effect_mappings_are_not_edited_and_proposals_can_be_approved(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    proposal = mappings["effect:GET /v1/charges/{id}"]
    response = client.put(f"{base(project_id)}/mappings/{proposal['id']}", headers=admin,
                          json={"transformation": {"effect": "read_only"}})
    assert response.status_code == 400
    assert "operations endpoint" in response.json()["error"]["message"]
    post(client, admin, project_id, f"/mappings/{proposal['id']}/approve")
    listed = operations(client, admin, project_id)["GET /v1/charges/{id}"]
    # Approving "unknown" does not make it read-only.
    assert (listed["declared_effect"], listed["effective_effect"]) == ("unknown", "unknown")


# ============================================================================= invariants


def test_invariants_are_stated_in_legacy_terms_only(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    invariants = by_key(every(client, admin, project_id, "/invariants"))
    assert len(invariants) == 28
    replacement_terms = ("payment", "transaction", "customer_id", "currency_code", "novapay",
                         "external_ref", "next_cursor", "page_size", "refund_id", "mapping")
    for item in invariants.values():
        blob = json.dumps([item["key"], item["definition"], item["endpoint"]]).lower()
        assert not [term for term in replacement_terms if term in blob], item["key"]
        for ref in item["evidence_refs"]:
            assert ref["filename"] in ("openapi.yaml", "fixtures.json")
            assert "novapay" not in ref.get("name", "")

    echo = invariants["echo:POST /v1/charges:body:$.customer->$.customer"]
    assert echo["confidence"] == "observed" and echo["status"] == "active"
    assert echo["definition"]["distinct_values"] == 2
    single = invariants["echo:POST /v1/charges:body:$.reference->$.reference"]
    # One fixture is not enough: it waits for approval before verification may use it.
    assert single["confidence"] == "inferred" and single["status"] == "proposed"
    schema = invariants["schema:POST /v1/charges:201"]["definition"]
    assert schema["required"] == ["$.amount", "$.created", "$.currency", "$.customer", "$.id",
                                  "$.status"]
    assert schema["enums"] == {"$.status": ["succeeded", "failed", "pending"]}
    paging = invariants["pagination:GET /v1/charges"]
    assert paging["definition"]["checks"] == ["page_holds_at_most_limit_records",
                                              "no_record_on_two_pages"]
    error = invariants["error:POST /v1/charges:create charge - invalid currency"]
    assert error["definition"]["status"] == 400
    assert error["definition"]["expect"] == {"$.error.code": "invalid_currency"}


def test_invariant_review(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    invariants = by_key(every(client, admin, project_id, "/invariants"))
    pending = [item for item in invariants.values() if item["status"] == "proposed"]
    assert len(pending) == 6 and all(item["confidence"] == "inferred" for item in pending)
    approved = post(client, admin, project_id, f"/invariants/{pending[0]['id']}/approve",
                    {"note": "confirmed"})
    assert approved["status"] == "active" and approved["confidence"] == "inferred"
    rejected = post(client, admin, project_id, f"/invariants/{pending[1]['id']}/reject")
    assert rejected["status"] == "rejected"
    actions = [a["action"] for a in get(client, admin, project_id, "/activity")["items"]]
    assert actions[:2] == ["invariant.reject", "invariant.approve"]
    post(client, admin, project_id, "/invariants/missing/approve", expect=404)


# ============================================================================= inventory


def test_the_inventory_counts_what_is_unknown_and_what_cannot_be_analysed(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    listing = get(client, admin, project_id, "/behaviours")
    behaviours = by_key(listing["items"])
    assert listing["total"] == listing["inventory_size"] == rebuilt["inventory_size"] == 27
    kinds: dict[str, int] = {}
    for item in behaviours.values():
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    assert kinds == {"response": 15, "auth": 6, "pagination_traversal": 1,
                     "pagination_termination": 1, "gap": 3, "unsupported": 1}
    # Documented without evidence: present, with nothing to support it.
    undocumented = behaviours["POST /v1/charges → 429"]
    assert undocumented["evidence_refs"] == [] and undocumented["has_evidence"] is False
    assert behaviours["POST /v1/charges → 201"]["has_evidence"] is True
    assert len(behaviours["POST /v1/charges → 201"]["evidence_refs"]) == 2
    assert behaviours["pagination termination: GET /v1/charges"]["evidence_refs"] == []
    assert len(behaviours["pagination traversal: GET /v1/charges"]["evidence_refs"]) == 2
    # Every gap is represented by exactly one behaviour.
    gaps = every(client, admin, project_id, "/gaps")
    assert all(gap["behaviour_key"] in behaviours for gap in gaps)
    own = [gap for gap in gaps if gap["behaviour_key"].startswith("gap: ")]
    assert len(own) == kinds["gap"] == 3
    # Evidence the product stored but cannot read is counted, at project level.
    notes = [item for item in behaviours.values() if item["kind"] == "unsupported"]
    assert notes[0]["group"] == "project" and notes[0]["endpoint"] is None
    assert "notes.md" in notes[0]["key"]
    assert notes[0]["unsupported_reason"].startswith("unsupported_by_product:")
    assert rebuilt["summary"]["behaviours"]["project_level"] == 1
    # Nothing has been verified, and the API does not pretend otherwise.
    assert {item["state"] for item in behaviours.values()} == {"not_assessed"}


def test_scope_can_be_marked_but_the_behaviour_stays_in_the_inventory(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    behaviours = by_key(every(client, admin, project_id, "/behaviours"))
    target = behaviours["POST /v1/charges → 429"]["id"]
    for body in ({"scope": "out_of_scope"}, {"scope": "out_of_scope", "note": "  "},
                 {"scope": "removed", "note": "x"}):
        patch(client, admin, project_id, f"/behaviours/{target}", body, expect=400)
    changed = patch(client, admin, project_id, f"/behaviours/{target}",
                    {"scope": "out_of_scope", "note": "Rate limiting is handled upstream."})
    assert changed["scope"] == "out_of_scope"
    assert changed["scope_note"] == "Rate limiting is handled upstream."
    after = get(client, admin, project_id, "/behaviours")
    assert after["total"] == 27 and after["inventory_fingerprint"] == rebuilt[
        "inventory_fingerprint"]
    assert by_key(after["items"])["POST /v1/charges → 429"]["scope"] == "out_of_scope"
    record = get(client, admin, project_id, "/activity")["items"][0]
    assert record["action"] == "behaviour.scope"
    assert record["metadata"]["scope"] == "out_of_scope"
    back = patch(client, admin, project_id, f"/behaviours/{target}", {"scope": "in_scope"})
    assert back["scope"] == "in_scope"


def test_gap_resolution(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    gap = every(client, admin, project_id, "/gaps")[0]
    patch(client, admin, project_id, f"/gaps/{gap['id']}", {"resolution": "accepted_risk"},
          expect=400)
    # Only new evidence can resolve a gap; a person cannot declare it resolved.
    patch(client, admin, project_id, f"/gaps/{gap['id']}",
          {"resolution": "resolved_by_evidence", "note": "trust me"}, expect=400)
    accepted = patch(client, admin, project_id, f"/gaps/{gap['id']}",
                     {"resolution": "accepted_risk", "note": "Known and tolerated."})
    assert accepted["resolution"] == "accepted_risk"
    assert accepted["resolution_note"] == "Known and tolerated."
    # Accepting the risk does not remove the behaviour from the inventory.
    assert get(client, admin, project_id, "/behaviours")["total"] == 27
    assert get(client, admin, project_id, "/activity")["items"][0]["action"] == "gap.resolve"


# ============================================================================= access


def test_comparison_shows_both_contracts_side_by_side(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    body = get(client, admin, project_id, "/comparison")
    assert len(body["pairs"]) == 6 and body["unpaired_legacy"] == []
    pair = next(p for p in body["pairs"] if p["legacy"]["path"] == "/v1/charges"
                and p["legacy"]["method"] == "POST")
    assert [f["path"] for f in pair["legacy"]["request_fields"]] == [
        "$.amount", "$.currency", "$.customer", "$.reference"]
    assert [f["path"] for f in pair["replacement"]["request_fields"]] == [
        "$.customer_id", "$.external_ref", "$.transaction.currency_code", "$.transaction.value"]
    assert pair["legacy"]["status_codes"] == ["201", "400", "401", "429"]
    kinds = {d["mapping_key"]: d["differences"] for d in pair["differences"]}
    assert kinds["field:POST /v1/charges:request.body:$.transaction.value"] == [
        "renamed", "nesting"]
    assert kinds["field:POST /v1/charges:response.body:$.created"] == [
        "renamed", "type"]
    assert body["project"][0]["differences"] == ["authentication"]


ROUTES = [
    ("post", "/reconstructions"), ("get", "/reconstructions"), ("get", "/reconstructions/latest"),
    ("get", "/reconstructions/x/behaviours"), ("get", "/comparison"), ("get", "/mappings"),
    ("get", "/mappings/x"), ("post", "/mappings/x/approve"), ("post", "/mappings/x/reject"),
    ("put", "/mappings/x"), ("post", "/mappings/bulk-approve"), ("get", "/gaps"),
    ("patch", "/gaps/x"), ("get", "/operations"), ("put", "/operations/x/effect"),
    ("get", "/invariants"), ("post", "/invariants/x/approve"), ("post", "/invariants/x/reject"),
    ("get", "/behaviours"), ("patch", "/behaviours/x"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_reconstruction_routes_require_the_admin_token(
    client: TestClient, project_id: str, method: str, path: str
) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.request(method, f"{base(project_id)}{path}", headers=headers, json={})
        assert response.status_code == 401, path
    # Authentication is checked before the project is looked up.
    response = client.request(method, f"/api/v1/projects/no-such-project{path}", json={})
    assert response.status_code == 401


def test_one_project_cannot_reach_another_projects_reconstruction(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict],
    rebuilt: dict
) -> None:
    other = client.post("/api/v1/projects", headers=admin, json={
        "name": "Other", "legacy_name": "A", "replacement_name": "B"}).json()["id"]
    mapping_id = mappings["auth"]["id"]
    behaviour = every(client, admin, project_id, "/behaviours")[0]["id"]
    gap = every(client, admin, project_id, "/gaps")[0]["id"]
    invariant = every(client, admin, project_id, "/invariants")[0]["id"]
    operation = get(client, admin, project_id, "/operations")["items"][0]["id"]
    for method, path, body in (
        ("get", f"/mappings/{mapping_id}", None),
        ("post", f"/mappings/{mapping_id}/approve", None),
        ("put", f"/mappings/{mapping_id}", {"transformation": {}}),
        ("post", "/mappings/bulk-approve", {"ids": [mapping_id]}),
        ("get", f"/reconstructions/{rebuilt['id']}/behaviours", None),
        ("patch", f"/behaviours/{behaviour}", {"scope": "in_scope"}),
        ("patch", f"/gaps/{gap}", {"resolution": "open"}),
        ("post", f"/invariants/{invariant}/approve", None),
        ("put", f"/operations/{operation}/effect", {"effect": "unknown", "note": "n"}),
        ("get", "/mappings", None), ("get", "/behaviours", None), ("get", "/operations", None),
    ):
        response = client.request(method, f"{base(other)}{path}", headers=admin, json=body)
        assert response.status_code == 404, (path, response.text)
    assert get(client, admin, project_id, f"/mappings/{mapping_id}")["status"] == "proposed"


def test_lists_are_paginated(
    client: TestClient, admin: dict[str, str], project_id: str, rebuilt: dict
) -> None:
    first = get(client, admin, project_id, "/mappings?limit=10")
    second = get(client, admin, project_id, "/mappings?limit=10&offset=10")
    assert first["total"] == 55 and len(first["items"]) == len(second["items"]) == 10
    assert not {m["id"] for m in first["items"]} & {m["id"] for m in second["items"]}
    assert client.get(f"{base(project_id)}/mappings?limit=500", headers=admin).status_code == 400
    assert len(every(client, admin, project_id, "/mappings?confidence=inferred")) == 8
    # Three error rules and the error envelope.
    assert len(every(client, admin, project_id, "/mappings?category=error")) == 4


def test_removed_evidence_stays_resolvable_in_an_earlier_reconstruction(
    client: TestClient, admin: dict[str, str], project_id: str, mappings: dict[str, dict]
) -> None:
    recordings = next(f for f in every(client, admin, project_id, "/evidence")
                      if f["filename"] == FILES["recordings"][0].name)
    client.delete(f"{base(project_id)}/evidence/{recordings['id']}", headers=admin)
    kept = get(client, admin, project_id,
               f"/mappings/{mappings['field:POST /v1/charges:request.body:$.customer_id']['id']}")
    removed = [ref for ref in kept["evidence_refs"] if ref["file_removed"]]
    assert removed and all(ref["filename"] == "recordings.json" for ref in removed)
