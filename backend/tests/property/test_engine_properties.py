"""Property tests of the transformation engine against the language specification
(docs/testing-strategy.md, "Engine property tests"). They test the engine, not a
project: nothing here contributes to any compatibility score."""

from __future__ import annotations

import base64
import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from revenant.adapter.validator import validate
from revenant.transform import engine
from revenant.transform.engine import MISSING, TransformError, apply_rules, convert
from revenant.transform.model import Pagination, Rule

# Derandomised so a CI run and a local run exercise the same cases.
settings.register_profile("revenant", max_examples=150, deadline=None, derandomize=True)
settings.load_profile("revenant")

RULES: TypeAdapter[list[Any]] = TypeAdapter(list[Rule])
CANARY = "CANARY-7f3a9c"
names = st.sampled_from(["a", "b", "c", "id", "value", "x-1", "n_2"])
scalars = st.one_of(st.none(), st.booleans(), st.integers(-10**9, 10**9),
                    st.floats(allow_nan=False, allow_infinity=False, width=32),
                    st.text(max_size=20))
values = st.recursive(scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=4), st.dictionaries(names, inner, max_size=4)), max_leaves=12)
pointers = st.lists(names, min_size=1, max_size=4).map(lambda parts: "$." + ".".join(parts))


@given(value=values, source=pointers, target=pointers)
def test_move_copies_any_value_unchanged(value: Any, source: str, target: str) -> None:
    body = engine.put(MISSING, source, value)
    output = apply_rules(RULES.validate_python(
        [{"op": "move", "from": f"body:{source}", "to": target}]), {"body": body}).output
    assert engine.get(output, target) == value
    assert json.dumps(engine.get(body, source)) == json.dumps(value)  # the source is untouched


@given(leaves=st.dictionaries(names, scalars, min_size=1, max_size=5), parent=names)
def test_nesting_then_flattening_is_the_identity(leaves: dict[str, Any], parent: str) -> None:
    nest = [{"op": "move", "from": f"body:$.{key}", "to": f"$.{parent}.inner.{key}"}
            for key in leaves]
    flat = [{"op": "move", "from": f"body:$.{parent}.inner.{key}", "to": f"$.{key}"}
            for key in leaves]
    nested = apply_rules(RULES.validate_python(nest), {"body": leaves}).output
    assert apply_rules(RULES.validate_python(flat), {"body": nested}).output == leaves


@given(seconds=st.integers(0, 4_102_444_800))
def test_time_conversions_round_trip(seconds: int) -> None:
    text = convert(seconds, "epoch_s_to_iso8601", {})
    assert convert(text, "iso8601_to_epoch_s", {}) == seconds
    assert convert(convert(text, "iso8601_to_epoch_ms", {}), "epoch_ms_to_iso8601", {}) == text


@given(number=st.integers(-10**18, 10**18))
def test_integer_and_string_round_trip(number: int) -> None:
    assert convert(convert(number, "string", {}), "integer", {}) == number
    assert convert(number, "scale", {"factor": "1"}) == number


@given(cents=st.integers(-10**12, 10**12))
def test_scaling_is_exact_or_refused(cents: int) -> None:
    try:
        scaled = convert(cents, "scale", {"factor": "0.01"})
    except TransformError as exc:
        assert exc.code == "precision_loss"
        return
    # Whatever was returned converts back to exactly the original.
    assert convert(scaled, "scale", {"factor": "100"}) == cents


@given(value=st.floats(allow_nan=False, allow_infinity=False))
def test_fractions_never_silently_become_integers(value: float) -> None:
    try:
        result = convert(value, "integer", {})
    except TransformError:
        assert value != int(value) or abs(value) > 10**30
    else:
        assert result == value


@given(declared=st.lists(st.text(min_size=1, max_size=6), min_size=1, max_size=5, unique=True),
       other=st.text(max_size=6),
       mode=st.sampled_from(["error", "passthrough", {"default": "fallback"}]))
def test_enum_map_is_total_over_declared_values(declared: list[str], other: str,
                                                mode: Any) -> None:
    mapping = {value: f"mapped:{index}" for index, value in enumerate(declared)}
    rule = RULES.validate_python([{"op": "enum_map", "from": "body:$.v", "to": "$.out",
                                   "map": mapping, "on_unmapped": mode}])
    for value in declared:
        assert apply_rules(rule, {"body": {"v": value}}).output == {"out": mapping[value]}
    if other in mapping:
        return
    try:
        output = apply_rules(rule, {"body": {"v": other}}).output
    except TransformError as exc:
        assert mode == "error" and exc.code == "unmapped_enum_value"
    else:
        assert mode != "error"
        assert output == {"out": other if mode == "passthrough" else "fallback"}


def paging(shape: Any = None, encoding: str = "base64_json", first: int = 1) -> Pagination:
    return Pagination.model_validate({
        "strategy": "computed_cursor",
        "legacy": {"page_param": "page", "limit_param": "limit", "first_page": first,
                   "default_limit": 20, "max_limit": 100},
        "upstream": {"cursor_param": "cursor", "size_param": "page_size"},
        "cursor": {"encoding": encoding, "shape": shape or {"page": "$page"}},
        "response": {"items_from": "body:$.items", "items_to": "$.data",
                     "has_more_from_presence_of": "body:$.next_cursor",
                     "has_more_to": "$.has_more", "echo": {"$.page": "$page"}}})


@given(page=st.integers(1, 10**6), limit=st.integers(1, 100))
def test_cursor_decodes_to_the_page_it_was_computed_from(page: int, limit: int) -> None:
    cursor = engine.encode_cursor(paging(), page, limit)
    assert json.loads(base64.b64decode(cursor)) == {"page": page}
    offset = engine.encode_cursor(paging({"skip": "$offset", "take": "$limit"}), page, limit)
    assert json.loads(base64.b64decode(offset)) == {"skip": (page - 1) * limit, "take": limit}
    assert engine.encode_cursor(paging("$page", "plain"), page, limit) == str(page)


class _Route:
    """The smallest route the response transformer needs: a list with pagination."""

    def __init__(self, pagination: Pagination) -> None:
        self.pagination = pagination
        self.response = type("Response", (), {
            "status_map": [], "headers": [], "errors": [],
            "body": type("Body", (), {"mode": "build", "rules": RULES.validate_python([
                {"op": "each", "from": "body:$.items", "to": "$.data", "rules": [
                    {"op": "move", "from": "item:$.n", "to": "$.id"}]}])})()})()


def _replacement(records: list[int], cursor: str, size: int) -> dict[str, Any]:
    """A cursor-paginated replacement, written independently of the engine."""
    page = json.loads(base64.b64decode(cursor))["page"]
    start = (page - 1) * size
    body: dict[str, Any] = {"items": [{"n": n} for n in records[start:start + size]]}
    if start + size < len(records):
        body["next_cursor"] = "more"
    return body


@given(total=st.integers(0, 60), limit=st.integers(1, 25))
def test_walking_legacy_pages_yields_every_record_once_in_order(total: int, limit: int) -> None:
    records = list(range(total))
    route = _Route(paging())
    seen: list[int] = []
    page = 1
    while True:
        query = {"page": page, "limit": limit}
        _, _, upstream = engine.paging_request(route.pagination, query)
        body = _replacement(records, upstream["cursor"], upstream["page_size"])
        legacy = engine.transform_response(route, [], 200, body, {"query": query})
        assert legacy.body["page"] == page and len(legacy.body["data"]) <= limit
        seen += [item["id"] for item in legacy.body["data"]]
        if not legacy.body["has_more"]:
            break
        page += 1
        assert page <= total + 2  # the walk terminates
    assert seen == records


# ---- arbitrary rule sets --------------------------------------------------------------------

sources = st.builds(lambda scope, pointer: f"{scope}:{pointer}",
                    st.sampled_from(["body", "query", "header", "path"]), pointers)
conditions = st.one_of(
    st.none(), st.builds(lambda path: {"exists": path}, sources),
    st.builds(lambda path, value: {"equals": [path, value]}, sources, scalars),
    st.builds(lambda path: {"not": {"exists": path}}, sources))


def _rule(kind: str, source: str, target: str, when: Any, value: Any, extra: str) -> dict:
    rule: dict[str, Any] = {"op": kind, "mapping_id": "m"}
    if when is not None:
        rule["when"] = when
    if kind in ("move", "convert", "enum_map"):
        rule.update({"from": source, "to": target})
    if kind == "convert":
        rule["as"] = extra
    if kind == "enum_map":
        rule.update(map={"a": 1, "b": value}, on_unmapped="passthrough")
    if kind in ("const", "default"):
        rule.update(to=target, value=value)
    if kind == "template":
        rule.update(to=target, template="{v}:{v}", vars={"v": source})
    if kind == "drop":
        rule["path"] = target
    return rule


simple_rules = st.builds(
    _rule, st.sampled_from(["move", "convert", "enum_map", "const", "default", "template",
                            "drop"]),
    sources, pointers, conditions, values,
    st.sampled_from(["string", "integer", "number", "boolean", "epoch_s_to_iso8601",
                     "iso8601_to_epoch_s"]))
loops = st.builds(
    lambda source, target, inner: {"op": "each", "from": source, "to": target, "mapping_id": "m",
                                   "rules": inner},
    sources, pointers,
    st.lists(st.builds(lambda name, target: {"op": "move", "from": f"item:$.{name}",
                                             "to": target, "mapping_id": "m"}, names, pointers),
             max_size=3))
rule_lists = st.lists(st.one_of(simple_rules, loops), max_size=8)


def _scopes(body: Any) -> dict[str, Any]:
    return {"body": body, "query": {"a": "1", "b": CANARY}, "path": {"id": "p1"},
            "header": {"a": "h", "authorization": f"Bearer {CANARY}", "cookie": CANARY,
                       "x-api-key": CANARY}}


@given(body=values, raw=rule_lists)
def test_the_engine_always_terminates_with_a_result_or_a_transform_error(
    body: Any, raw: list[dict]
) -> None:
    rules = RULES.validate_python(raw)
    try:
        result = apply_rules(rules, _scopes(body))
    except TransformError as exc:
        assert exc.code and exc.rule_index is not None
        return
    assert len(result.trace) == len(rules)
    assert all(entry["outcome"] in ("applied", "skipped_missing", "skipped_condition")
               for entry in result.trace)
    # Traces never hold values.
    assert CANARY not in json.dumps(result.trace)


@given(body=st.dictionaries(names, values, max_size=4), raw=rule_lists)
def test_nothing_is_emitted_from_a_place_no_rule_referenced(body: dict, raw: list[dict]) -> None:
    """A field that no rule names must never appear in the output."""
    referenced = json.dumps(raw)
    planted = {**body, "unreferenced_field": CANARY, "deep": {"unreferenced_too": [CANARY]}}
    assert "unreferenced" not in referenced and '"deep"' not in referenced
    scopes = {"body": planted, "query": {"zz_unreferenced": CANARY}, "path": {},
              "header": {"zz-unreferenced": CANARY}}
    try:
        output = apply_rules(RULES.validate_python(raw), scopes).output
    except TransformError:
        return
    if output is not MISSING:
        assert CANARY not in json.dumps(output)


def _document(body_rules: list[dict], header_rules: list[dict]) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "project_slug": "p",
        "upstream": {"base_url": "https://api.example.com"},
        "auth": {"mapping_id": "m", "inbound": {"type": "api_key_header", "header": "X-API-Key"},
                 "outbound": {"type": "bearer", "secret_ref": "token"}},
        "routes": [{
            "id": "r", "mapping_id": "m", "effect": "unknown",
            "match": {"method": "POST", "path": "/v1/things/{id}"},
            "upstream": {"method": "POST", "path": "/things/{id}"},
            "request": {"path": [{"op": "move", "from": "path:$.id", "to": "$.id",
                                  "mapping_id": "m", "required": True}],
                        "headers": header_rules, "body": {"rules": body_rules}}}],
    }


carrier_sources = st.sampled_from([
    "header:$.authorization", "header:$.Authorization", "header:$.cookie", "header:$.X-API-Key",
    "header:$.x-api-key", "header:$.proxy-authorization", "header:$.a", "body:$.a", "query:$.a"])
header_targets = st.sampled_from(["$.Authorization", "$.cookie", "$.X-Api-Key", "$.X-Trace",
                                  "$.set-cookie", "$.accept"])
carrier_rules = st.lists(st.one_of(
    st.builds(lambda source, target: {"op": "move", "from": source, "to": target,
                                      "mapping_id": "m"}, carrier_sources, pointers),
    st.builds(lambda source, target: {"op": "template", "to": target, "template": "{v}",
                                      "vars": {"v": source}, "mapping_id": "m"},
              carrier_sources, pointers),
    st.builds(lambda source: {"op": "const", "to": "$.x", "value": 1, "mapping_id": "m",
                              "when": {"exists": source}}, carrier_sources)), max_size=5)
header_rules = st.lists(st.builds(
    lambda source, target: {"op": "move", "from": source, "to": target, "mapping_id": "m"},
    st.sampled_from(["body:$.a", "header:$.a", "query:$.a"]), header_targets), max_size=3)


@given(body_rules=carrier_rules, headers=header_rules)
def test_no_accepted_document_can_read_or_emit_a_credential_carrier(
    body_rules: list[dict], headers: list[dict]
) -> None:
    document, problems = validate(_document(body_rules, headers))
    touches = [rule for rule in body_rules if any(
        marker in json.dumps(rule).lower()
        for marker in ("authorization", "cookie", "x-api-key"))]
    written = [rule for rule in headers if rule["to"][2:].lower() in (
        "authorization", "cookie", "set-cookie", "x-api-key")]
    if touches or written:
        assert problems, "a rule touching a credential carrier was accepted"
        return
    if problems or document is None:
        return  # rejected for another reason (for example two rules writing one target)
    route = document.routes[0]
    scopes = engine.request_scopes(route, "/v1/things/7", {"a": "q"},
                                   {"A": "h", "Authorization": f"Bearer {CANARY}",
                                    "Cookie": CANARY, "X-API-Key": CANARY}, {"a": "b"})
    try:
        upstream = engine.transform_request(route, scopes)
    except TransformError:
        return
    sent = json.dumps([upstream.path, upstream.query, upstream.headers, upstream.body])
    assert CANARY not in sent
    assert not {name.lower() for name in upstream.headers} & {
        "authorization", "cookie", "set-cookie", "x-api-key"}


@given(body=values, body_rules=rule_lists)
def test_documents_the_validator_accepts_execute_without_internal_errors(
    body: Any, body_rules: list[dict]
) -> None:
    document, problems = validate(_document(body_rules, []))
    if problems or document is None:
        return
    route = document.routes[0]
    scopes = engine.request_scopes(route, "/v1/things/7", {"a": "1"}, {"a": "h"}, body)
    try:
        upstream = engine.transform_request(route, scopes)
    except TransformError:
        return
    assert upstream.path == "/things/7" and upstream.method == "POST"
    json.dumps(upstream.body)  # the output is always plain JSON
