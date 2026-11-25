"""The reconstruction engine on small hand-built contracts (requirements 3, 4.5, 8.4, 8.35)."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from revenant.reconstruction import correlate, invariants, inventory, names, pagination
from revenant.reconstruction.contract import (
    EndpointContract,
    FieldInfo,
    FixtureInfo,
    ParamInfo,
    flatten_values,
    template_values,
)
from revenant.reconstruction.effects import effective_effect, read_only_objections
from revenant.reconstruction.engine import reconstruct
from revenant.reconstruction.pairing import pair_endpoints, score
from revenant.reconstruction.shapes import validate_transformation
from revenant.reconstruction.types import GapOut
from revenant.transform.model import check_pointer, split_path

REF = {"type": "fact", "evidence_file_id": "f", "locator": "#/x", "fact_type": "field",
       "subject": "s", "basis": "declared"}
_counter = iter(range(10_000))


def field(path: str, kind: str = "string", required: bool = True,
          enum: tuple[Any, ...] | None = None) -> FieldInfo:
    return FieldInfo(path, kind, required, enum, False, None, None, REF)


def fields(*items: FieldInfo) -> dict[str, FieldInfo]:
    return {item.path: item for item in items}


def param(name: str, role: str | None = None, **extra: Any) -> ParamInfo:
    return ParamInfo("query", name, "integer", False, "default" in extra, extra.get("default"),
                     extra.get("minimum"), extra.get("maximum"), role, REF)


def endpoint(side: str, method: str, path: str, **extra: Any) -> EndpointContract:
    return EndpointContract(id=f"{side}-{method}-{path}", side=side, method=method, path=path,
                            operation_id=None, summary="", declared=True, **extra)


def fixture(method: str, path: str, status: int = 200, body: Any = None, request: Any = None,
            query: dict[str, Any] | None = None, pair_with: str | None = None) -> FixtureInfo:
    number = next(_counter)
    return FixtureInfo(
        id=f"fx{number}", name=f"fixture {number}", evidence_file_id="file",
        request={"method": method, "path": path, "query": query or {}, "body": request},
        response={"status": status, "body": body}, pair_with=pair_with, variable_fields=())


def paired(legacy: EndpointContract, replacement: EndpointContract, legacy_path: str,
           replacement_path: str, legacy_body: Any, replacement_body: Any, *, status: int = 200,
           replacement_status: int | None = None, legacy_request: Any = None,
           replacement_request: Any = None, legacy_query: dict[str, Any] | None = None,
           replacement_query: dict[str, Any] | None = None) -> None:
    ours = fixture(legacy.method, legacy_path, status, legacy_body, legacy_request, legacy_query)
    theirs = fixture(replacement.method, replacement_path, replacement_status or status,
                     replacement_body, replacement_request, replacement_query, pair_with=ours.id)
    legacy.fixtures.append(ours)
    replacement.fixtures.append(theirs)


def run(*contracts: EndpointContract) -> Any:
    return reconstruct(list(contracts), [])


def keyed(items: list[Any]) -> dict[str, Any]:
    return {item.key: item for item in items}


# ============================================================================= names, values


def test_name_normalisation() -> None:
    assert names.tokens("customerId") == names.tokens("customer_id") == ("customer",)
    assert names.same_name("receipt_number", "receipt_no")
    assert names.same_name("created", "created_at")
    assert not names.same_name("at", "occurred_at")
    assert not names.same_name("type", "kind")
    assert names.tokens("id") == ("id",)  # a suffix is never stripped down to nothing
    assert names.same_path("$.items[].customer_id", "$.items[].customer")
    assert not names.same_path("$.items[].customer", "$.items.customer")


def test_values_are_flattened_in_document_order() -> None:
    assert flatten_values({"a": {"b": 1}, "list": [{"x": 1}, {"x": 2}], "bad key": 3}) == {
        "$.a.b": [1], "$.list[].x": [1, 2]}
    assert flatten_values(None) == {}
    assert template_values("/v1/charges/{id}/refund", "/v1/charges/P-1/refund") == {"id": "P-1"}
    assert template_values("/v1/charges/{id}", "/v1/other/P-1") is None


def test_correlation_requires_agreement_in_every_observation() -> None:
    observations = [({"body:$.a": ["x"], "body:$.b": ["x"]}, {"body:$.t": ["x"]}),
                    ({"body:$.a": ["y"], "body:$.b": ["z"]}, {"body:$.t": ["y"]})]
    found = correlate.candidates(observations, "body:$.t")
    assert [(c.source, c.conversion, c.distinct) for c in found] == [("body:$.a", "identity", 2)]
    # Present on one side only: not the same field.
    lopsided = [({"body:$.a": ["x"]}, {"body:$.t": ["x"]}), ({"body:$.a": ["y"]}, {})]
    assert correlate.candidates(lopsided, "body:$.t") == []


def test_correlation_ignores_values_that_prove_nothing() -> None:
    for value in (True, False, None, ""):
        assert correlate.candidates([({"body:$.a": [value]}, {"body:$.t": [value]})],
                                    "body:$.t") == []
    weak = correlate.candidates([({"body:$.a": [2]}, {"body:$.t": [2]})], "body:$.t")
    assert weak[0].weak is True
    strong = correlate.candidates([({"body:$.a": [500]}, {"body:$.t": [500]})], "body:$.t")
    assert strong[0].weak is False
    # 1 is not True, and "1" is not 1 without a declared conversion.
    assert correlate.candidates([({"body:$.a": [1]}, {"body:$.t": [True]})], "body:$.t") == []


def test_conversions_are_detected_only_when_they_reproduce_the_value_exactly() -> None:
    def conversion(source: Any, target: Any) -> list[str]:
        return [c.conversion for c in correlate.candidates(
            [({"body:$.s": [source]}, {"body:$.t": [target]})], "body:$.t")]

    assert conversion(1741944067, "2025-03-14T09:21:07Z") == ["epoch_s_to_iso8601"]
    assert conversion("2025-03-14T09:21:07Z", 1741944067) == ["iso8601_to_epoch_s"]
    assert conversion(1741944067000, "2025-03-14T09:21:07Z") == ["epoch_ms_to_iso8601"]
    assert conversion(1741944067, "2025-03-14T09:21:08Z") == []
    assert conversion(1741944067, "2025-03-14 09:21:07") == []
    assert conversion(42, "42") == ["string"]
    assert conversion("42", 42) == ["integer"]


def test_enum_maps_must_be_consistent_and_one_to_one() -> None:
    source, target = ("A", "B", "C"), ("x", "y", "z")
    good = [({"s": ["A", "B"]}, {"t": ["x", "y"]}), ({"s": ["A"]}, {"t": ["x"]})]
    assert correlate.enum_map(good, "s", "t", source, target) == ({"A": "x", "B": "y"}, (0, 1))
    assert correlate.enum_map([({"s": ["A", "A"]}, {"t": ["x", "y"]})], "s", "t",
                              source, target) is None
    assert correlate.enum_map([({"s": ["A", "B"]}, {"t": ["x", "x"]})], "s", "t",
                              source, target) is None
    assert correlate.enum_map([({"s": ["Q"]}, {"t": ["x"]})], "s", "t", source, target) is None


# ============================================================================= pairing


def test_recordings_pair_endpoints_and_similarity_is_only_inferred() -> None:
    legacy_a, legacy_b = endpoint("legacy", "GET", "/v1/charges/{id}"), endpoint(
        "legacy", "GET", "/v1/charges/{id}/events")
    replacement_a, replacement_b = endpoint("replacement", "GET", "/payments/{pid}"), endpoint(
        "replacement", "GET", "/payments/{pid}/events")
    paired(legacy_a, replacement_a, "/v1/charges/1", "/payments/1", {"id": "1"}, {"pid": "1"})
    pairs, unpaired = pair_endpoints([legacy_a, legacy_b], [replacement_a, replacement_b])
    found = {p.legacy.path: (p.replacement.path, p.confidence) for p in pairs}
    assert found == {"/v1/charges/{id}": ("/payments/{pid}", "observed"),
                     "/v1/charges/{id}/events": ("/payments/{pid}/events", "inferred")}
    assert unpaired == []


def test_pairs_are_not_forced() -> None:
    lonely = endpoint("legacy", "DELETE", "/v1/webhooks/{id}")
    pairs, unpaired = pair_endpoints([lonely], [endpoint("replacement", "GET", "/payments")])
    assert pairs == [] and unpaired[0][1] == "no replacement endpoint is similar enough"

    tied = endpoint("legacy", "GET", "/v1/things")
    options = [endpoint("replacement", "GET", "/things"), endpoint("replacement", "GET", "/thing")]
    assert score(tied, options[0]) == score(tied, options[1])
    pairs, unpaired = pair_endpoints([tied], options)
    assert pairs == [] and "match equally well" in unpaired[0][1]

    # Two legacy endpoints wanting the same replacement: neither gets it by guesswork.
    twins = [endpoint("legacy", "GET", "/v1/items"), endpoint("legacy", "GET", "/v2/items")]
    pairs, unpaired = pair_endpoints(twins, [endpoint("replacement", "GET", "/items")])
    assert pairs == [] and len(unpaired) == 2

    result = run(lonely, endpoint("replacement", "GET", "/payments"))
    gap = keyed(result.gaps)["endpoint:DELETE /v1/webhooks/{id}:unpaired"]
    assert gap.severity == "blocking"
    assert not [p for p in result.proposals if p.category == "endpoint"]


# ============================================================================= proposers


def test_names_alone_give_inferred_mappings_and_only_identity_is_observed() -> None:
    legacy = endpoint("legacy", "POST", "/v1/orders", request_fields=fields(
        field("$", "object"), field("$.amount", "integer"), field("$.customer"), field("$.note"),
        field("$.when")))
    replacement = endpoint("replacement", "POST", "/orders", request_fields=fields(
        field("$", "object"), field("$.amount", "integer"), field("$.customer_id"),
        field("$.meta", "object"), field("$.meta.note"), field("$.when", "integer")))
    result = run(legacy, replacement)
    found = {p.key.rsplit(":", 1)[1]: p for p in result.proposals
             if p.category == "request_field"}
    assert (found["$.amount"].confidence, found["$.amount"].rule_name) == (
        "observed", "identical_name_and_type")
    assert (found["$.customer_id"].confidence, found["$.customer_id"].rule_name) == (
        "inferred", "normalised_name")
    assert (found["$.meta.note"].confidence, found["$.meta.note"].rule_name) == (
        "inferred", "structural_match")
    # Same name, different type, no fixture to establish the conversion: a gap.
    assert "$.when" not in found
    gap = keyed(result.gaps)["request_field:POST /v1/orders:required"]
    assert gap.severity == "blocking" and "type differs" in gap.description
    assert all(p.requires_approval for p in found.values() if p.confidence == "inferred")


def test_fixtures_that_contradict_a_name_match_win() -> None:
    legacy = endpoint("legacy", "POST", "/v1/orders", request_fields=fields(
        field("$", "object"), field("$.customer")))
    replacement = endpoint("replacement", "POST", "/orders", request_fields=fields(
        field("$", "object"), field("$.customer_id")))
    paired(legacy, replacement, "/v1/orders", "/orders", None, None,
           legacy_request={"customer": "C1"}, replacement_request={"customer_id": "internal-9"})
    result = run(legacy, replacement)
    assert not [p for p in result.proposals if p.category == "request_field"]
    assert "no field in the paired recordings carries its value" in keyed(result.gaps)[
        "request_field:POST /v1/orders:required"].description


def test_ambiguous_correlation_is_not_resolved_by_guessing() -> None:
    legacy = endpoint("legacy", "GET", "/v1/a", responses={"200": fields(
        field("$", "object"), field("$.total", "integer"))}, statuses={"200": REF})
    replacement = endpoint("replacement", "GET", "/a", responses={"200": fields(
        field("$", "object"), field("$.gross", "integer"), field("$.net", "integer"))},
        statuses={"200": REF})
    paired(legacy, replacement, "/v1/a", "/a", {"total": 500}, {"gross": 500, "net": 500})
    result = run(legacy, replacement)
    assert not [p for p in result.proposals if p.category == "response_field"]
    gap = keyed(result.gaps)["response_field:GET /v1/a:unmapped"]
    assert "several fields carry the same values" in gap.description


def test_effect_proposals() -> None:
    def proposed(legacy_method: str, replacement_method: str,
                 marker: str | None = None) -> tuple[str, str]:
        legacy = endpoint("legacy", legacy_method, "/v1/things/{id}")
        replacement = endpoint("replacement", replacement_method, "/things/{id}",
                               effect_marker=(marker, REF) if marker else None)
        # A recording pairs the two, so the methods are free to differ.
        paired(legacy, replacement, "/v1/things/1", "/things/1", {"id": "1"}, {"id": "1"})
        effect = next(p for p in run(legacy, replacement).proposals if p.category == "effect")
        return effect.transformation["effect"], effect.confidence

    assert proposed("GET", "GET") == ("unknown", "inferred")
    assert proposed("HEAD", "OPTIONS") == ("unknown", "inferred")
    assert proposed("GET", "POST") == ("state_changing", "observed")
    assert proposed("DELETE", "GET") == ("state_changing", "observed")
    assert proposed("GET", "GET", "state_changing") == ("state_changing", "observed")
    # A read-only marker is evidence for the reviewer, never a classification.
    assert proposed("GET", "GET", "read_only") == ("unknown", "inferred")
    assert proposed("POST", "POST", "read_only") == ("state_changing", "observed")


@pytest.mark.parametrize(("legacy", "replacement", "declared", "expected"), [
    ("GET", "GET", None, "unknown"), ("GET", "GET", "unknown", "unknown"),
    ("GET", "GET", "read_only", "read_only"), ("GET", "GET", "state_changing", "state_changing"),
    ("POST", "GET", "read_only", "state_changing"), ("GET", "PUT", "read_only", "state_changing"),
    ("HEAD", "OPTIONS", "read_only", "read_only"), ("get", "get", "read_only", "read_only"),
    ("DELETE", "DELETE", None, "state_changing"), ("GET", "PATCH", "unknown", "state_changing"),
    ("GET", "GET", "nonsense", "unknown"),
])
def test_effective_effect_uses_methods_as_a_floor(
    legacy: str, replacement: str, declared: str | None, expected: str
) -> None:
    assert effective_effect(legacy, replacement, declared) == expected


def test_read_only_objections() -> None:
    assert read_only_objections("GET", "GET", []) == []
    assert read_only_objections("GET", "GET", [("GET /a", "read_only")]) == []
    assert len(read_only_objections("POST", "DELETE", [])) == 2
    assert read_only_objections("GET", "GET", [("GET /a", "state_changing")]) == [
        "the specification marks GET /a as state-changing"]


# ============================================================================= pagination


def cursor(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def paginated(pages: list[tuple[int, int, Any, bool]]) -> tuple[EndpointContract,
                                                               EndpointContract]:
    """(page, limit, recorded cursor, has more) for each paired recording."""
    legacy = endpoint(
        "legacy", "GET", "/v1/items",
        params=[param("page", "page", minimum=1), param("limit", "limit", default=20)],
        responses={"200": fields(field("$", "object"), field("$.data", "array"), field("$.data[]"),
                                 field("$.has_more", "boolean"), field("$.page", "integer"))},
        statuses={"200": REF})
    replacement = endpoint(
        "replacement", "GET", "/items",
        params=[param("cursor", "cursor"), param("page_size", "limit")],
        responses={"200": fields(field("$", "object"), field("$.items", "array"),
                                 field("$.items[]"),
                                 field("$.next_cursor", required=False))},
        statuses={"200": REF})
    for page, limit, recorded, more in pages:
        items = [f"item-{page}-{n}" for n in range(limit if more else 1)]
        paired(legacy, replacement, "/v1/items", "/items",
               {"data": items, "has_more": more, "page": page},
               {"items": items, **({"next_cursor": "n"} if more else {})},
               legacy_query={"page": page, "limit": limit},
               replacement_query={"cursor": recorded, "page_size": limit})
    return legacy, replacement


def paging(result: Any) -> Any:
    return next((p for p in result.proposals if p.category == "pagination"), None)


def test_pagination_is_observed_only_when_every_part_was_recorded() -> None:
    complete = run(*paginated([(1, 3, cursor({"page": 1}), True), (2, 3, cursor({"page": 2}),
                                                                 False)]))
    mapping = paging(complete)
    assert mapping.confidence == "observed"
    assert mapping.transformation["cursor"] == {"encoding": "base64_json",
                                                "shape": {"page": "$page"}}
    assert mapping.transformation["response"]["echo"] == {"$.page": "$page"}
    assert "pagination:GET /v1/items:termination" not in keyed(complete.gaps)

    partial = run(*paginated([(1, 3, cursor({"page": 1}), True), (2, 3, cursor({"page": 2}),
                                                                True)]))
    assert paging(partial).confidence == "inferred"
    assert keyed(partial.gaps)["pagination:GET /v1/items:termination"].behaviour_key == \
        "pagination termination: GET /v1/items"

    single = run(*paginated([(2, 3, cursor({"page": 2}), True)]))
    assert paging(single).confidence == "inferred"


def test_one_recorded_page_does_not_establish_the_cursor_rule() -> None:
    """Everything else is fully recorded here; only the number of pages is short."""
    legacy, replacement = paginated([(2, 3, cursor({"page": 2}), True),
                                     (2, 3, cursor({"page": 2}), False)])
    del legacy.responses["200"]["$.page"]
    for item in legacy.fixtures:
        del item.response["body"]["page"]
    mapping = paging(run(legacy, replacement))
    assert "echo" not in mapping.transformation["response"]
    assert "for both values" in mapping.rationale
    assert "recordings of 1 page" in mapping.rationale
    assert mapping.confidence == "inferred"


def test_offset_cursors_and_plain_cursors_are_recognised() -> None:
    offset = run(*paginated([(1, 5, cursor({"skip": 0}), True), (3, 5, cursor({"skip": 10}),
                                                                  False)]))
    assert paging(offset).transformation["cursor"]["shape"] == {"skip": "$offset"}
    plain = run(*paginated([(2, 5, "2", True), (3, 5, "3", False)]))
    assert paging(plain).transformation["cursor"] == {"encoding": "plain", "shape": "$page"}


@pytest.mark.parametrize("pages", [
    [],                                                      # nothing recorded
    [(1, 3, "opaque-token-abc", True), (2, 3, "opaque-token-def", True)],
    [(1, 3, cursor({"page": 7}), True), (2, 3, cursor({"page": 9}), True)],
    [(2, 2, cursor({"n": 2}), True)],                        # page or limit? cannot tell
])
def test_an_unestablished_cursor_is_a_gap_not_a_guess(pages: list[Any]) -> None:
    result = run(*paginated(pages))
    assert paging(result) is None
    gap = keyed(result.gaps)["pagination:GET /v1/items"]
    assert gap.severity == "blocking" and "cursor" in gap.description
    assert pagination.is_paginated(paginated([])[0])


# ============================================================================= invariants


def test_echo_invariants_need_two_fixtures_with_different_values() -> None:
    legacy = endpoint("legacy", "POST", "/v1/orders")
    legacy.fixtures += [
        fixture("POST", "/v1/orders", 201, {"customer": "C1", "note": "n", "flag": True},
                {"customer": "C1", "note": "n", "flag": True}),
        fixture("POST", "/v1/orders", 201, {"customer": "C2", "note": "n"},
                {"customer": "C2", "note": "n"}),
        fixture("POST", "/v1/orders", 400, {"customer": "WRONG"}, {"customer": "C3"}),
    ]
    found = keyed(invariants.derive([legacy]))
    assert found["echo:POST /v1/orders:body:$.customer->$.customer"].confidence == "observed"
    # The same value twice shows less than two different values.
    assert found["echo:POST /v1/orders:body:$.note->$.note"].confidence == "inferred"
    assert not [key for key in found if "flag" in key]
    assert not [key for key in found if "$.customer->$.note" in key]


def test_invariants_never_read_the_replacement_side() -> None:
    with pytest.raises(ValueError, match="legacy side only"):
        invariants.derive([endpoint("replacement", "GET", "/payments")])
    legacy = endpoint("legacy", "GET", "/v1/a", responses={"200": fields(
        field("$", "object"), field("$.id"), field("$.state", enum=("a", "b")))},
        statuses={"200": REF})
    alone = invariants.derive([legacy])
    with_replacement = run(legacy, endpoint("replacement", "GET", "/a")).invariants
    assert [(i.key, i.definition) for i in alone] == [
        (i.key, i.definition) for i in with_replacement]
    assert alone[0].definition == {"status": 200, "required": ["$.id", "$.state"],
                                   "types": {"$": "object", "$.id": "string",
                                             "$.state": "string"},
                                   "enums": {"$.state": ["a", "b"]}}


def test_a_pagination_invariant_the_fixtures_contradict_is_not_emitted() -> None:
    legacy, _ = paginated([(1, 2, cursor({"page": 1}), True), (2, 2, cursor({"page": 2}), True)])
    assert [i.kind for i in invariants.derive([legacy])].count("pagination") == 1
    legacy.fixtures[1].response["body"]["data"] = legacy.fixtures[0].response["body"]["data"]
    assert [i.kind for i in invariants.derive([legacy])].count("pagination") == 0


# ============================================================================= inventory


def test_inventory_enumeration_and_fingerprint() -> None:
    legacy = endpoint("legacy", "GET", "/v1/a", statuses={"200": REF, "404": REF, "default": REF},
                      auth=[("ApiKey", {"type": "apiKey"}, REF)],
                      unsupported=[("response[200]", ["text/csv"], REF)])
    legacy.fixtures.append(fixture("GET", "/v1/a", 500, {"e": 1}))
    represented = GapOut("g1", "status_code", legacy.id, legacy.name, "t", "d", "m", "advisory",
                         behaviour_key="GET /v1/a → 404")
    own = GapOut("g2", "auth", None, None, "project gap", "d", "m", "blocking")
    found = keyed(inventory.enumerate_behaviours([legacy], [represented, own], []))
    assert set(found) == {"GET /v1/a → 200", "GET /v1/a → 404", "GET /v1/a → 500",
                          "auth: GET /v1/a", "unsupported: GET /v1/a response[200]", "gap: g2"}
    assert found["GET /v1/a → 500"].evidence_refs and not found["GET /v1/a → 404"].evidence_refs
    assert found["gap: g2"].endpoint_ref is None and own.behaviour_key == "gap: g2"
    assert represented.behaviour_key == "GET /v1/a → 404"
    assert "text/csv" in (found["unsupported: GET /v1/a response[200]"].unsupported_reason or "")

    assert inventory.fingerprint(["b", "a"]) == inventory.fingerprint(["a", "b"])
    assert inventory.fingerprint(["a"]) != inventory.fingerprint(["a", "b"])
    assert inventory.difference(None, ["a"]) == {"previous_size": None, "size": 1, "added": [],
                                                 "removed": []}
    assert inventory.difference(["a", "b"], ["b", "c"]) == {
        "previous_size": 2, "size": 2, "added": ["c"], "removed": ["a"]}


def test_the_engine_is_deterministic_whatever_order_the_contracts_arrive_in() -> None:
    def build() -> list[EndpointContract]:
        legacy, replacement = paginated([(1, 3, cursor({"page": 1}), True)])
        return [legacy, replacement, endpoint("legacy", "GET", "/v1/a"),
                endpoint("replacement", "GET", "/a")]

    def shape(result: Any) -> Any:
        strip = lambda refs: [(r["type"], r.get("locator")) for r in refs]  # noqa: E731
        return ([(p.key, p.transformation, p.confidence, p.rationale, strip(p.evidence_refs))
                 for p in result.proposals],
                [(g.key, g.description) for g in result.gaps],
                [b.key for b in result.behaviours], result.summary)

    assert shape(run(*build())) == shape(run(*reversed(build())))


# ============================================================================= rule shapes


def test_pointer_and_path_grammar() -> None:
    for good in ("$", "$.a", "$.a.b-c[0].d_1"):
        assert check_pointer(good) == good
    for bad in ("", "a", "$.", "$.a[*]", "$..a", "$.a[]", "$.a b", "$['a']", "$.a;drop"):
        with pytest.raises(ValueError):
            check_pointer(bad)
    with pytest.raises(ValueError, match="deeper"):
        check_pointer("$" + ".a" * 17)
    assert split_path("request.body:$.a") == ("request.body", "$.a")
    with pytest.raises(ValueError):
        split_path("$.a")


GOOD = [
    ("request_field", {"section": "request.body", "rule": {
        "op": "move", "from": "body:$.a", "to": "$.b", "when": {"exists": "query:$.x"}}}),
    ("response_field", {"section": "response.body", "each": {"from": "body:$.items", "to": "$.d"},
                        "rule": {"op": "move", "from": "item:$.a", "to": "$.b"}}),
    ("response_field", {"section": "response.body", "rule": {
        "op": "move", "from": "request.query:$.page", "to": "$.page"}}),
    ("enum", {"section": "response.body", "rule": {
        "op": "enum_map", "from": "body:$.s", "to": "$.t", "map": {"A": "a"},
        "on_unmapped": {"default": "other"}}}),
    ("header", {"section": "request.headers", "rule": {
        "op": "template", "to": "$.X-Trace", "template": "{a}-{b}",
        "vars": {"a": "body:$.a", "b": "header:$.x-request-id"}}}),
    ("status_code", {"section": "response.status", "from": 200, "to": 201}),
    ("endpoint", {"match": {"method": "GET", "path": "/a/{id}"},
                  "upstream": {"method": "GET", "path": "/b/{bid}"},
                  "path_params": [{"from": "path:$.id", "to": "bid"}]}),
    ("auth", {"inbound": {"type": "api_key_query", "param": "key"},
              "outbound": {"type": "basic", "secret_ref": "upstream"}}),
    ("error", {"kind": "error_rule", "when": {"status_in": [404]}, "status": 404,
               "vars": {"code": {"const": "gone"}}}),
]
BAD = [
    ("request_field", {"section": "request.body", "rule": {"op": "exec", "cmd": "id"}}),
    ("request_field", {"section": "anywhere", "rule": {"op": "const", "to": "$.a", "value": 1}}),
    ("request_field", {"section": "request.body", "rule": {
        "op": "move", "from": "request.body:$.a", "to": "$.b"}}),  # response-only scope
    ("response_field", {"section": "response.body", "rule": {
        "op": "move", "from": "item:$.a", "to": "$.b"}}),          # item outside each
    ("header", {"section": "response.headers", "rule": {
        "op": "const", "to": "$.Set-Cookie", "value": "x"}}),
    ("query", {"section": "request.query", "rule": {
        "op": "move", "from": "header:$.Cookie", "to": "$.c"}}),
    ("request_field", {"section": "request.body", "rule": {
        "op": "move", "from": "body:$.a", "to": "$.b", "when": {}}}),
    ("status_code", {"section": "response.status", "from": 200, "to": 9000}),
    ("endpoint", {"match": {"method": "FETCH", "path": "/a"},
                  "upstream": {"method": "GET", "path": "/b"}}),
    ("endpoint", {"match": {"method": "GET", "path": "/a/{id}"},
                  "upstream": {"method": "GET", "path": "/b"},
                  "path_params": [{"from": "path:$.id", "to": "nowhere"}]}),
    ("auth", {"inbound": {"type": "api_key_header"},
              "outbound": {"type": "bearer", "secret_ref": "x"}}),
    ("auth", {"inbound": {"type": "none"}, "outbound": {"type": "bearer"}}),
    ("auth", {"inbound": {"type": "none"},
              "outbound": {"type": "bearer", "secret_ref": "x", "value": "s3cret"}}),
    ("error", {"kind": "error_rule", "when": {"status_in": [404]}, "status": 200, "vars": {}}),
    ("error", {"kind": "error_rule", "when": {"status_in": [404]}, "status": 404,
               "vars": {"code": {"const": "a", "from": "body:$.a"}}}),
    ("pagination", {"strategy": "computed_cursor",
                    "legacy": {"page_param": "page", "limit_param": "limit"},
                    "upstream": {"cursor_param": "c"},
                    "cursor": {"encoding": "base64_json", "shape": {"page": "$page * 2"}},
                    "response": {"items_from": "body:$.i", "items_to": "$.d"}}),
    ("effect", {"effect": "read_only"}),
    ("request_field", "not an object"),
]


@pytest.mark.parametrize(("category", "data"), GOOD)
def test_valid_transformations_are_accepted(category: str, data: dict) -> None:
    assert validate_transformation(category, data) == []


@pytest.mark.parametrize(("category", "data"), BAD)
def test_invalid_transformations_are_rejected(category: str, data: Any) -> None:
    assert validate_transformation(category, data) != []
