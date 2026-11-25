"""The interpreter and the static validator (requirements 5.3, 5.4, 5.7, 6.1 to 6.8, 6.12)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import TypeAdapter

from revenant.adapter.validator import Context, validate
from revenant.transform import engine
from revenant.transform.engine import MISSING, TransformError, apply_rules, convert
from revenant.transform.model import ErrorRule, Pagination, Rule

RULES: TypeAdapter[list[Any]] = TypeAdapter(list[Rule])


def rules(*items: dict[str, Any]) -> list[Any]:
    return RULES.validate_python(list(items))


def build(body: Any, *items: dict[str, Any], **scopes: Any) -> Any:
    return apply_rules(rules(*items), {"body": body, **scopes}).output


def outcomes(body: Any, *items: dict[str, Any]) -> list[str]:
    return [entry["outcome"] for entry in apply_rules(rules(*items), {"body": body}).trace]


# ============================================================================= operations


def test_move_renames_nests_and_flattens() -> None:
    assert build({"amount": 5, "currency": "ZAR"},
                 {"op": "move", "from": "body:$.amount", "to": "$.transaction.value"},
                 {"op": "move", "from": "body:$.currency", "to": "$.transaction.currency_code"},
                 ) == {"transaction": {"value": 5, "currency_code": "ZAR"}}
    assert build({"a": {"b": {"c": [1, 2]}}},
                 {"op": "move", "from": "body:$.a.b.c[1]", "to": "$.second"}) == {"second": 2}
    assert build({"a": None}, {"op": "move", "from": "body:$.a", "to": "$.b"}) == {"b": None}


def test_output_starts_empty_and_holds_only_mapped_values() -> None:
    source = {"keep": 1, "secret_note": "do not forward", "nested": {"x": 1}}
    result = apply_rules(rules({"op": "move", "from": "body:$.keep", "to": "$.kept"}),
                         {"body": source})
    assert result.output == {"kept": 1}
    assert engine.unmapped_fields(source, result.consumed) == [
        "body:$.nested.x", "body:$.secret_note"]
    assert apply_rules([], {"body": source}).output is MISSING


def test_the_source_is_never_modified_and_output_shares_nothing_with_it() -> None:
    source = {"list": [{"a": 1}], "obj": {"k": "v"}}
    before = copy.deepcopy(source)
    output = build(source, {"op": "move", "from": "body:$.obj", "to": "$.copy"},
                   {"op": "move", "from": "body:$.list", "to": "$.items"})
    output["copy"]["k"] = "changed"
    output["items"][0]["a"] = 99
    assert source == before


def test_missing_sources_are_skipped_or_fail_when_required() -> None:
    assert outcomes({}, {"op": "move", "from": "body:$.a", "to": "$.b"}) == ["skipped_missing"]
    with pytest.raises(TransformError) as excinfo:
        build({}, {"op": "const", "to": "$.x", "value": 1},
              {"op": "move", "from": "body:$.a", "to": "$.b", "required": True})
    assert excinfo.value.code == "missing_required" and excinfo.value.rule_index == 1
    assert "body:$.a" in excinfo.value.message


def test_const_default_and_drop() -> None:
    assert build({}, {"op": "const", "to": "$.kind", "value": {"a": [1]}}) == {"kind": {"a": [1]}}
    assert build({"a": 1}, {"op": "move", "from": "body:$.a", "to": "$.x"},
                 {"op": "default", "to": "$.x", "value": 9},
                 {"op": "default", "to": "$.y", "value": 9}) == {"x": 1, "y": 9}
    passthrough = apply_rules(rules({"op": "drop", "path": "$.internal"}), {"body": {}},
                              start={"keep": 1, "internal": 2}).output
    assert passthrough == {"keep": 1}


def test_conditions() -> None:
    body = {"kind": "card", "n": 2, "flag": False}

    def applied(when: dict[str, Any], status: int = 200) -> bool:
        result = apply_rules(rules({"op": "const", "to": "$.x", "value": 1, "when": when}),
                             {"body": body, "status": status})
        return result.trace[0]["outcome"] == "applied"

    assert applied({"exists": "body:$.kind"}) and not applied({"exists": "body:$.nope"})
    assert applied({"equals": ["body:$.kind", "card"]})
    assert not applied({"equals": ["body:$.kind", "cash"]})
    assert not applied({"equals": ["body:$.nope", None]})
    assert applied({"in": ["body:$.n", [1, 2]]}) and not applied({"in": ["body:$.n", [3]]})
    assert applied({"status_in": [200]}) and not applied({"status_in": [404]})
    assert applied({"not": {"exists": "body:$.nope"}})
    assert not applied({"exists": "body:$.kind", "equals": ["body:$.n", 3]})  # a conjunction
    # False is not 0 and 1 is not True.
    assert applied({"equals": ["body:$.flag", False]})
    assert not applied({"equals": ["body:$.flag", 0]})


def test_each_maps_every_element_in_order() -> None:
    body = {"items": [{"id": "a", "v": 1, "extra": True}, {"id": "b", "v": 2}, {}]}
    result = apply_rules(rules({"op": "each", "from": "body:$.items", "to": "$.data", "rules": [
        {"op": "move", "from": "item:$.id", "to": "$.ref"},
        {"op": "move", "from": "item:$.v", "to": "$.amount.value"}]}), {"body": body})
    assert result.output == {"data": [{"ref": "a", "amount": {"value": 1}},
                                      {"ref": "b", "amount": {"value": 2}}, {}]}
    assert engine.unmapped_fields(body, result.consumed) == ["body:$.items[].extra"]
    assert build({"items": []}, {"op": "each", "from": "body:$.items", "to": "$.d",
                                 "rules": []}) == {"d": []}
    with pytest.raises(TransformError, match="not an array"):
        build({"items": {"0": 1}}, {"op": "each", "from": "body:$.items", "to": "$.d",
                                    "rules": []})
    with pytest.raises(TransformError, match="too many"):
        build({"items": [0] * (engine.MAX_ARRAY + 1)},
              {"op": "each", "from": "body:$.items", "to": "$.d", "rules": []})


def test_enum_map_obeys_on_unmapped() -> None:
    def mapped(value: Any, on_unmapped: Any) -> Any:
        return build({"s": value}, {"op": "enum_map", "from": "body:$.s", "to": "$.t",
                                    "map": {"COMPLETED": "succeeded", "3": "three"},
                                    "on_unmapped": on_unmapped})

    assert mapped("COMPLETED", "error") == {"t": "succeeded"}
    assert mapped(3, "error") == {"t": "three"}
    assert mapped("OTHER", "passthrough") == {"t": "OTHER"}
    assert mapped("OTHER", {"default": "unknown"}) == {"t": "unknown"}
    with pytest.raises(TransformError) as excinfo:
        mapped("OTHER", "error")
    assert excinfo.value.code == "unmapped_enum_value"
    assert "OTHER" not in excinfo.value.message  # errors carry no values


def test_template_interpolates_named_values_once() -> None:
    assert build({"a": "x", "n": 7}, {"op": "template", "to": "$.t", "template": "{a}-{n}/{a}",
                                      "vars": {"a": "body:$.a", "n": "body:$.n"}}) == {
        "t": "x-7/x"}
    # A value that looks like a placeholder is not expanded again.
    assert build({"a": "{b}", "b": "SECRET"},
                 {"op": "template", "to": "$.t", "template": "{a}",
                  "vars": {"a": "body:$.a", "b": "body:$.b"}}) == {"t": "{b}"}
    assert outcomes({}, {"op": "template", "to": "$.t", "template": "{a}",
                         "vars": {"a": "body:$.a"}}) == ["skipped_missing"]
    with pytest.raises(TransformError):
        build({"a": {"x": 1}}, {"op": "template", "to": "$.t", "template": "{a}",
                                "vars": {"a": "body:$.a"}})


@pytest.mark.parametrize(("value", "kind", "params", "expected"), [
    (5, "string", {}, "5"), (True, "string", {}, "true"), ("x", "string", {}, "x"),
    (1.5, "string", {}, "1.5"),
    ("42", "integer", {}, 42), (42.0, "integer", {}, 42), (7, "integer", {}, 7),
    ("1.50", "number", {}, 1.5), ("3", "number", {}, 3),
    ("true", "boolean", {}, True), (False, "boolean", {}, False),
    (1741944067, "epoch_s_to_iso8601", {}, "2025-03-14T09:21:07Z"),
    ("2025-03-14T09:21:07Z", "iso8601_to_epoch_s", {}, 1741944067),
    (1741944067000, "epoch_ms_to_iso8601", {}, "2025-03-14T09:21:07Z"),
    ("2025-03-14T09:21:07Z", "iso8601_to_epoch_ms", {}, 1741944067000),
    (500, "scale", {"factor": "0.01"}, 5), (1999, "scale", {"factor": "0.01"}, 19.99),
    (19.99, "scale", {"factor": 100}, 1999), ("12.5", "scale", {"factor": "2"}, 25),
])
def test_conversions(value: Any, kind: str, params: dict, expected: Any) -> None:
    result = convert(value, kind, params)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize(("value", "kind", "params", "code"), [
    (1.5, "integer", {}, "precision_loss"), ("abc", "integer", {}, "conversion_failed"),
    (True, "integer", {}, "conversion_failed"), ("NaN", "number", {}, "conversion_failed"),
    ("1e400", "number", {}, "conversion_failed"), ("yes", "boolean", {}, "conversion_failed"),
    (1, "boolean", {}, "conversion_failed"), ([1], "string", {}, "conversion_failed"),
    (None, "string", {}, "conversion_failed"),
    (1741944067001, "epoch_ms_to_iso8601", {}, "precision_loss"),
    ("2025-03-14 09:21:07", "iso8601_to_epoch_s", {}, "conversion_failed"),
    ("2025-03-14T09:21:07+02:00", "iso8601_to_epoch_s", {}, "conversion_failed"),
    (10 ** 30, "epoch_s_to_iso8601", {}, "conversion_failed"),
    (1.5, "epoch_s_to_iso8601", {}, "conversion_failed"),
    (1, "scale", {"factor": "0.3333333333333333333333"}, "precision_loss"),
    (10 ** 20, "scale", {"factor": "1e20"}, "conversion_failed"),
    (1, "scale", {}, "conversion_failed"), (1, "scale", {"factor": "x"}, "conversion_failed"),
    (1, "python", {}, "unknown_conversion"),
])
def test_conversions_fail_explicitly_rather_than_guess(
    value: Any, kind: str, params: dict, code: str
) -> None:
    with pytest.raises(TransformError) as excinfo:
        convert(value, kind, params)
    assert excinfo.value.code == code


def test_writes_that_conflict_are_errors() -> None:
    with pytest.raises(TransformError) as excinfo:
        build({"a": 1, "b": 2}, {"op": "move", "from": "body:$.a", "to": "$.x"},
              {"op": "move", "from": "body:$.b", "to": "$.x.y"})
    assert excinfo.value.code == "target_conflict"
    with pytest.raises(TransformError):
        build({"a": 1}, {"op": "move", "from": "body:$.a", "to": "$.list[3]"})
    assert build({"a": 1, "b": 2}, {"op": "move", "from": "body:$.a", "to": "$.list[0]"},
                 {"op": "move", "from": "body:$.b", "to": "$.list[1]"}) == {"list": [1, 2]}


def test_header_scope_is_case_insensitive_and_traces_hold_no_values() -> None:
    result = apply_rules(
        rules({"op": "move", "from": "header:$.X-Request-Id", "to": "$.trace",
               "mapping_id": "m1"}),
        {"body": None, "header": {"x-request-id": "VALUE-123"}})
    assert result.output == {"trace": "VALUE-123"}
    assert result.trace == [{"rule_index": 0, "op": "move", "mapping_id": "m1",
                             "from": "header:$.X-Request-Id", "to": "$.trace",
                             "outcome": "applied"}]
    assert "VALUE-123" not in str(result.trace)


def test_oversized_strings_are_refused() -> None:
    with pytest.raises(TransformError) as excinfo:
        build({"a": "x" * (engine.MAX_STRING + 1)},
              {"op": "move", "from": "body:$.a", "to": "$.b"})
    assert excinfo.value.code == "limit_exceeded"


# ============================================================================= pagination


def paging(**cursor: Any) -> Pagination:
    return Pagination.model_validate({
        "strategy": "computed_cursor",
        "legacy": {"page_param": "page", "limit_param": "limit", "first_page": 1,
                   "default_limit": 20, "max_limit": 100},
        "upstream": {"cursor_param": "cursor", "size_param": "page_size"},
        "cursor": cursor or {"encoding": "base64_json", "shape": {"page": "$page"}},
        "response": {"items_from": "body:$.items", "items_to": "$.data",
                     "has_more_from_presence_of": "body:$.next_cursor",
                     "has_more_to": "$.has_more", "echo": {"$.page": "$page",
                                                           "$.limit": "$limit"}}})


def test_the_documented_cursor_example() -> None:
    assert engine.encode_cursor(paging(), 2, 20) == "eyJwYWdlIjoyfQ=="
    assert engine.paging_request(paging(), {"page": "2", "limit": 5}) == (
        2, 5, {"cursor": "eyJwYWdlIjoyfQ==", "page_size": 5})
    assert engine.paging_request(paging(), {})[:2] == (1, 20)
    assert engine.encode_cursor(paging(encoding="plain", shape="$offset"), 3, 10) == "20"


@pytest.mark.parametrize("query", [{"page": 0}, {"page": -1}, {"page": "two"}, {"page": True},
                                   {"page": 1.5}, {"limit": 0}, {"limit": 101}, {"limit": "x"},
                                   {"page": "٣"}])
def test_invalid_legacy_paging_is_refused(query: dict) -> None:
    with pytest.raises(TransformError) as excinfo:
        engine.paging_request(paging(), query)
    assert excinfo.value.code == "invalid_pagination"


class _Section:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def listing(errors: list[Any] | None = None) -> Any:
    body = _Section(mode="build", rules=rules(
        {"op": "each", "from": "body:$.items", "to": "$.data", "rules": [
            {"op": "move", "from": "item:$.n", "to": "$.id"}]}))
    return _Section(pagination=paging(), response=_Section(
        status_map=[], headers=[], body=body, errors=errors or []))


def test_has_more_follows_the_presence_of_a_real_marker() -> None:
    def has_more(body: dict[str, Any]) -> Any:
        return engine.transform_response(listing(), [], 200, body,
                                         {"query": {"page": 2, "limit": 5}}).body

    assert has_more({"items": [{"n": 1}], "next_cursor": "abc"}) == {
        "data": [{"id": 1}], "has_more": True, "page": 2, "limit": 5}
    assert has_more({"items": [{"n": 1}]})["has_more"] is False
    # A marker that is present but null does not announce another page.
    assert has_more({"items": [], "next_cursor": None}) == {
        "data": [], "has_more": False, "page": 2, "limit": 5}


def test_an_error_no_rule_matches_keeps_its_status_and_invents_nothing() -> None:
    envelope = rules({"op": "move", "from": "var:$.code", "to": "$.error.code"})
    known = TypeAdapter(list[ErrorRule]).validate_python([{
        "when": {"status_in": [404], "equals": ["body:$.code", "GONE"]}, "status": 410,
        "vars": {"code": {"const": "gone"}, "detail": {"from": "body:$.missing"}}}])
    matched = engine.transform_response(listing(known), envelope, 404, {"code": "GONE"}, {})
    assert (matched.status, matched.body, matched.unmapped_error) == (
        410, {"error": {"code": "gone"}}, False)
    for status, body in ((404, {"code": "OTHER"}), (500, {"code": "GONE"}), (503, None)):
        unmapped = engine.transform_response(listing(known), envelope, status, body, {})
        assert (unmapped.status, unmapped.body, unmapped.unmapped_error) == (status, None, True)


# ============================================================================= validator


def document(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0", "project_slug": "p",
        "upstream": {"base_url": "https://api.example.com"},
        "auth": {"mapping_id": "a", "inbound": {"type": "api_key_header", "header": "X-API-Key"},
                 "outbound": {"type": "bearer", "secret_ref": "token"}},
        "error_envelope": {"mapping_id": "e", "rules": [
            {"op": "move", "from": "var:$.code", "to": "$.error.code", "mapping_id": "e"}]},
        "routes": [{
            "id": "get-thing", "mapping_id": "r", "effect": "unknown",
            "match": {"method": "GET", "path": "/v1/things/{id}"},
            "upstream": {"method": "GET", "path": "/things/{thing_id}"},
            "request": {"path": [{"op": "move", "from": "path:$.id", "to": "$.thing_id",
                                  "required": True, "mapping_id": "r"}]},
            "response": {"status_map": [{"from": 200, "to": 200, "mapping_id": "s"}],
                         "body": {"rules": [{"op": "move", "from": "body:$.a", "to": "$.b",
                                             "mapping_id": "f"}]}},
        }],
    }
    route = base["routes"][0]
    for key, value in changes.items():
        target: Any = base
        parts = key.split("__")
        if parts[0] == "route":
            target, parts = route, parts[1:]
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return base


def problems(**changes: Any) -> str:
    return " | ".join(validate(document(**changes), Context(
        approved_mapping_ids={"a", "e", "r", "s", "f"}))[1])


def test_a_well_formed_document_is_accepted() -> None:
    assert problems() == ""
    parsed, found = validate(document())
    assert parsed is not None and found == []


def rule(**fields: Any) -> dict[str, Any]:
    return {"op": "move", "from": "body:$.a", "to": "$.b", "mapping_id": "f", **fields}


BROKEN = [
    ({"schema_version": "2.0"}, "schema_version"),
    ({"surprise": 1}, "surprise"),
    ({"route__request__body": {"rules": [{"op": "exec", "cmd": "id"}]}}, "op"),
    ({"route__request__body": {"rules": [rule(mapping_id=None)]}}, "no mapping_id"),
    ({"route__request__body": {"rules": [rule(mapping_id="zzz")]}}, "not an approved mapping"),
    ({"route__request__body": {"rules": [rule(), rule()]}}, "two unconditional rules write $.b"),
    ({"route__request__body": {"rules": [rule(**{"from": "request.body:$.a"})]}},
     "cannot be read here"),
    ({"route__request__body": {"rules": [rule(**{"from": "header:$.X-API-Key"})]}},
     "credential carrier"),
    ({"route__request__body": {"rules": [rule(**{"from": "header:$.authorization"})]}},
     "credential carrier"),
    ({"route__request__headers": [rule(to="$.Authorization")]}, "credential carrier"),
    ({"route__request__headers": [rule(to="$.x-api-key")]}, "credential carrier"),
    ({"route__request__headers": [rule(to="$.Cookie")]}, "credential carrier"),
    ({"route__request__path": []}, "no rule produces the path parameter {thing_id}"),
    ({"route__request__body": {"rules": [rule(**{"from": "path:$.other"})]}},
     "not a parameter of the legacy path"),
    ({"route__effect": "read_only"}, "no mapping_id"),
    ({"route__effect": "read_only", "route__effect_mapping_id": "f",
      "route__upstream": {"method": "POST", "path": "/things/{thing_id}"}},
     "declared read_only but uses POST"),
    ({"route__effect": "harmless"}, "effect"),
    ({"route__match": {"method": "FETCH", "path": "/x"}}, "not an HTTP method"),
    ({"route__response__status_map": [{"from": 200, "to": 200, "mapping_id": "s"},
                                      {"from": 200, "to": 201, "mapping_id": "s"}]},
     "maps one upstream status twice"),
    ({"route__response__errors": [{"when": {"status_in": [404]}, "status": 404, "vars": {},
                                   "mapping_id": None}]}, "no mapping_id"),
    ({"upstream": {"base_url": "ftp://example.com"}}, "http or https"),
    ({"auth": {"mapping_id": "a", "inbound": {"type": "none"},
               "outbound": {"type": "bearer", "secret_ref": "t"}}},
     "allow_unauthenticated_runtime"),
    ({"auth": {"mapping_id": "a", "inbound": {"type": "api_key_header", "header": "K"},
               "outbound": {"type": "bearer", "secret_ref": "t", "value": "s3cr3t"}}}, "value"),
    ({"auth": {"mapping_id": None, "inbound": {"type": "api_key_header", "header": "K"},
               "outbound": {"type": "bearer", "secret_ref": "t"}}}, "no mapping_id"),
]


@pytest.mark.parametrize(("changes", "expected"), BROKEN)
def test_invalid_documents_are_rejected_with_the_reason(changes: dict, expected: str) -> None:
    assert expected in problems(**changes)


def test_query_credential_carriers_are_outside_the_language() -> None:
    auth = {"mapping_id": "a", "inbound": {"type": "api_key_query", "param": "api_key"},
            "outbound": {"type": "api_key_query", "param": "token", "secret_ref": "t"}}
    assert "cannot be read" in problems(auth=auth, route__request__body={"rules": [
        rule(**{"from": "query:$.api_key"})]})
    assert "cannot be read" in problems(auth=auth, route__response__body={"rules": [
        rule(**{"from": "request.query:$.API_KEY"})]})
    for name in ("token", "api_key"):
        assert "cannot be written" in problems(auth=auth, route__request__query=[
            rule(to=f"$.{name}")])
    page = paging().model_dump(by_alias=True)
    page["mapping_id"] = "f"
    page["upstream"]["cursor_param"] = "token"
    assert "pagination writes the credential carrier token" in problems(
        auth=auth, route__pagination=page)
    page["upstream"]["cursor_param"] = "cursor"
    page["legacy"]["page_param"] = "api_key"
    assert "pagination reads the credential carrier" in problems(auth=auth,
                                                                 route__pagination=page)


def test_every_failure_is_listed_and_required_fields_are_checked() -> None:
    found = validate(document(
        route__effect="read_only",
        route__match={"method": "DELETE", "path": "/v1/things/{id}"},
        route__request__headers=[rule(to="$.Authorization", mapping_id=None)]))[1]
    assert len(found) >= 4
    context = Context(required_request_fields={"get-thing": {"$.b", "$.c.d", "$.e"}})
    body = {"rules": [rule(), {"op": "const", "to": "$.c", "value": {"d": 1},
                               "mapping_id": "f"}]}
    result = validate(document(route__request__body=body), context)[1]
    assert result == ["route get-thing: required replacement request fields are not produced: "
                      "$.e"]
    assert validate(document(), Context(base_url_problem="resolves to a private address"))[1] == [
        "upstream.base_url: resolves to a private address"]
    assert validate("nope")[1] == ["document: must be an object"]
    assert "larger than" in validate(document(project_slug="x" * 600_000))[1][0]
