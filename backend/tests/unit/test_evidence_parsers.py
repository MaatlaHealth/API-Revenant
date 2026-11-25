"""Upload validation and the OpenAPI, Swagger and fixture parsers (requirement 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from revenant.evidence.fixtures import (
    is_credential_name,
    parse_fixture_file,
    template_score,
)
from revenant.evidence.loader import (
    EvidenceRejected,
    UnparseableDocument,
    check_upload,
    detect_kind,
    load_structured,
    safe_filename,
)
from revenant.evidence.openapi import ParsedEndpoint, ParsedSpec, parse_spec

DEMO = Path(__file__).resolve().parents[3] / "demo"
LIMIT = 5 * 1024 * 1024


def gravepay() -> ParsedSpec:
    return parse_spec(yaml.safe_load((DEMO / "gravepay" / "openapi.yaml").read_text("utf-8")))


def novapay() -> ParsedSpec:
    return parse_spec(yaml.safe_load((DEMO / "novapay" / "openapi.yaml").read_text("utf-8")))


def endpoint(spec: ParsedSpec, method: str, path: str) -> ParsedEndpoint:
    return next(e for e in spec.endpoints if (e.method, e.path) == (method, path))


def facts(item: ParsedEndpoint, fact_type: str) -> dict[str, dict]:
    return {f.subject: f.value for f in item.facts if f.fact_type == fact_type}


# ============================================================================= uploads


@pytest.mark.parametrize("name", ["spec.exe", "archive.zip", "page.html", "noextension",
                                  "script.py", "data.xml"])
def test_disallowed_file_types_are_rejected(name: str) -> None:
    with pytest.raises(EvidenceRejected) as excinfo:
        check_upload(name, b"{}", LIMIT)
    assert excinfo.value.code == "unsupported_evidence"


def test_oversized_upload_is_rejected() -> None:
    with pytest.raises(EvidenceRejected) as excinfo:
        check_upload("spec.json", b"x" * 101, 100)
    assert excinfo.value.code == "payload_too_large"


@pytest.mark.parametrize("raw", [b"", b"   \n", b"\x00\x01binary",
                                 b"\xff\xfe\x00bad utf8 \xc3\x28"])
def test_empty_or_non_text_uploads_are_rejected(raw: bytes) -> None:
    with pytest.raises(EvidenceRejected):
        check_upload("spec.json", raw, LIMIT)


def test_utf8_bom_is_accepted() -> None:
    assert check_upload("spec.json", b"\xef\xbb\xbf{}", LIMIT) == "{}"


@pytest.mark.parametrize("given, expected", [
    ("../../etc/passwd.json", "passwd.json"),
    ("C:\\Users\\someone\\spec.yaml", "spec.yaml"),
    ("a/b/c/spec.json", "spec.json"),
    (None, "upload"),
    ("", "upload"),
    ("we<ird>:na|me?.json", "weirdname.json"),
])
def test_filenames_are_reduced_to_a_safe_base_name(given: str | None, expected: str) -> None:
    assert safe_filename(given) == expected


def test_malformed_json_and_yaml_are_reported_not_raised_as_crashes() -> None:
    with pytest.raises(UnparseableDocument) as bad_json:
        load_structured('{"a": ', "spec.json")
    assert "not valid JSON" in bad_json.value.problems[0]
    with pytest.raises(UnparseableDocument) as bad_yaml:
        load_structured("a: [1, 2\nb: }", "spec.yaml")
    assert "not valid YAML" in bad_yaml.value.problems[0]


def test_yaml_cannot_construct_python_objects() -> None:
    hostile = "!!python/object/apply:os.system ['echo owned']"
    with pytest.raises(UnparseableDocument):
        load_structured(hostile, "spec.yaml")


def test_deeply_nested_documents_are_refused() -> None:
    nested = "[" * 80 + "]" * 80
    with pytest.raises(UnparseableDocument) as excinfo:
        load_structured(nested, "spec.json")
    assert "nested" in excinfo.value.problems[0]
    extreme = "[" * 100_000 + "]" * 100_000
    with pytest.raises(UnparseableDocument):
        load_structured(extreme, "spec.json")


def test_yaml_alias_expansion_is_bounded() -> None:
    bomb = "\n".join(
        ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
        + [f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]"
           for i in range(8)]
    )
    with pytest.raises(UnparseableDocument) as excinfo:
        load_structured(bomb, "bomb.yaml")
    assert "more than" in excinfo.value.problems[0]


@pytest.mark.parametrize("document, filename, expected", [
    ({"openapi": "3.0.3", "paths": {}}, "a.yaml", "openapi"),
    ({"swagger": "2.0", "paths": {}}, "a.json", "swagger"),
    ({"revenant_fixtures": "1.0", "fixtures": []}, "a.json", "fixtures"),
    ({"$schema": "http://json-schema.org/draft-07/schema#"}, "a.json", "json_schema"),
    ({"type": "object", "properties": {}}, "a.json", "json_schema"),
    ("anything", "notes.md", "markdown"),
    ({"hello": "world"}, "a.json", None),
    ([1, 2, 3], "a.json", None),
    ({"openapi": "3.0.3"}, "a.json", None),
])
def test_evidence_kind_is_detected_from_content(document, filename: str, expected) -> None:
    assert detect_kind(document, filename) == expected


# ============================================================================= OpenAPI 3


def test_gravepay_endpoints_and_methods_are_extracted() -> None:
    spec = gravepay()
    assert spec.spec_format == "openapi"
    assert spec.title.startswith("GravePay") and spec.version == "1.4"
    assert sorted((e.method, e.path) for e in spec.endpoints) == [
        ("GET", "/v1/charges"),
        ("GET", "/v1/charges/{id}"),
        ("GET", "/v1/charges/{id}/events"),
        ("GET", "/v1/charges/{id}/receipt"),
        ("POST", "/v1/charges"),
        ("POST", "/v1/charges/{id}/refund"),
    ]
    assert spec.unresolved_refs == [] and spec.warnings == []


def test_request_fields_types_and_required_flags() -> None:
    fields = facts(endpoint(gravepay(), "POST", "/v1/charges"), "field")
    assert fields["request:$.customer"] == {"type": "string", "required": True}
    assert fields["request:$.amount"] == {"type": "integer", "required": True, "minimum": 1}
    assert fields["request:$.reference"] == {"type": "string", "required": False}
    assert fields["response[201]:$.status"]["enum"] == ["succeeded", "failed", "pending"]
    assert fields["response[201]:$.created"]["format"] == "date-time"
    assert fields["response[400]:$.error.code"] == {"type": "string", "required": True}


def test_nested_and_array_fields_are_flattened() -> None:
    nova = facts(endpoint(novapay(), "POST", "/payments"), "field")
    assert nova["request:$.transaction.value"]["type"] == "integer"
    assert nova["request:$.transaction.currency_code"]["required"] is True
    listing = facts(endpoint(gravepay(), "GET", "/v1/charges"), "field")
    assert listing["response[200]:$.data"]["type"] == "array"
    assert listing["response[200]:$.data[].id"] == {"type": "string", "required": True}
    assert listing["response[200]:$.has_more"]["type"] == "boolean"


def test_every_fact_has_a_locator_into_the_document() -> None:
    document = yaml.safe_load((DEMO / "gravepay" / "openapi.yaml").read_text("utf-8"))
    spec = parse_spec(document)
    count = 0
    for item in spec.endpoints:
        for fact in item.facts:
            assert fact.locator.startswith("#/"), fact
            node = document
            for token in fact.locator[2:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                node = node[int(token)] if isinstance(node, list) else node[token]
            count += 1
    assert count > 100


def test_referenced_schema_fields_point_at_their_definition() -> None:
    item = endpoint(gravepay(), "POST", "/v1/charges")
    locators = {f.subject: f.locator for f in item.facts if f.fact_type == "field"}
    assert locators["request:$.customer"] == (
        "#/components/schemas/ChargeRequest/properties/customer")
    assert locators["response[400]:$.error.code"] == (
        "#/components/schemas/Error/properties/error/properties/code")


def test_status_codes_parameters_pagination_and_auth() -> None:
    spec = gravepay()
    create = endpoint(spec, "POST", "/v1/charges")
    assert set(facts(create, "status_code")) == {"201", "400", "401", "429"}
    assert facts(create, "status_code")["429"]["has_schema"] is False
    assert set(facts(create, "error_shape")) == {"400", "401"}
    assert facts(create, "auth") == {"ApiKey": {"type": "apiKey", "in": "header",
                                               "name": "X-API-Key"}}

    listing = endpoint(spec, "GET", "/v1/charges")
    assert facts(listing, "pagination") == {
        "page": {"role": "page", "type": "integer", "required": False, "default": 1,
                 "minimum": 1},
        "limit": {"role": "limit", "type": "integer", "required": False, "default": 20,
                  "minimum": 1, "maximum": 100},
    }
    nova_list = endpoint(novapay(), "GET", "/payments")
    assert {name: value["role"] for name, value in facts(nova_list, "pagination").items()} == {
        "cursor": "cursor", "page_size": "limit"}
    assert facts(nova_list, "auth") == {"Bearer": {"type": "http", "scheme": "bearer"}}

    retrieve = endpoint(spec, "GET", "/v1/charges/{id}")
    assert facts(retrieve, "parameter")["path:id"] == {"type": "string", "required": True}


def test_declared_effect_extension_is_recorded_as_evidence() -> None:
    receipt = endpoint(novapay(), "GET", "/payments/{payment_id}/receipt")
    assert facts(receipt, "effect") == {"declared": {"effect": "state_changing"}}
    assert facts(endpoint(novapay(), "GET", "/payments/{payment_id}"), "effect") == {}


def test_stored_schemas_are_self_contained() -> None:
    item = endpoint(gravepay(), "POST", "/v1/charges")
    assert "$ref" not in json.dumps(item.request_schema)
    assert item.request_schema["properties"]["amount"]["type"] == "integer"
    assert "$ref" not in json.dumps(item.response_schemas)


def _spec(paths: dict, **extra) -> dict:
    return {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": paths, **extra}


def test_remote_and_file_references_are_never_followed() -> None:
    document = _spec({"/a": {"get": {"responses": {"200": {
        "description": "ok", "content": {"application/json": {"schema": {
            "type": "object", "properties": {
                "remote": {"$ref": "https://evil.example.com/schema.json#/x"},
                "file": {"$ref": "../other.yaml#/components/schemas/Y"},
                "missing": {"$ref": "#/components/schemas/DoesNotExist"},
                "ok": {"type": "string"}}}}}}}}}})
    spec = parse_spec(document)
    assert len(spec.unresolved_refs) == 3
    assert any("evil.example.com" in ref and "not fetched" in ref for ref in spec.unresolved_refs)
    assert any("DoesNotExist" in ref for ref in spec.unresolved_refs)
    fields = facts(spec.endpoints[0], "field")
    assert fields["response[200]:$.remote"]["type"] == "unresolved"
    assert fields["response[200]:$.ok"]["type"] == "string"


def test_cyclic_references_terminate() -> None:
    document = _spec(
        {"/tree": {"get": {"responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}}}}},
        components={"schemas": {"Node": {"type": "object", "properties": {
            "name": {"type": "string"},
            "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}}}}}},
    )
    spec = parse_spec(document)
    fields = facts(spec.endpoints[0], "field")
    assert "response[200]:$.name" in fields and "response[200]:$.children" in fields
    assert any("cyclic" in warning for warning in spec.warnings)
    assert len(spec.endpoints[0].facts) < 50


def test_self_referencing_pointer_terminates() -> None:
    document = _spec(
        {"/x": {"get": {"responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/A"}}}}}}}},
        components={"schemas": {"A": {"$ref": "#/components/schemas/B"},
                                "B": {"$ref": "#/components/schemas/A"}}},
    )
    spec = parse_spec(document)
    assert any("cyclic" in warning for warning in spec.warnings)


def test_all_of_is_merged_and_one_of_is_reported_as_a_union() -> None:
    document = _spec({"/x": {"post": {"requestBody": {"content": {"application/json": {"schema": {
        "allOf": [{"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                  {"type": "object", "properties": {"b": {"oneOf": [{"type": "string"},
                                                                    {"type": "integer"}]}}}]
    }}}}, "responses": {"204": {"description": "done"}}}}})
    fields = facts(parse_spec(document).endpoints[0], "field")
    assert fields["request:$.a"] == {"type": "string", "required": True}
    assert fields["request:$.b"] == {"type": "union", "required": False}


def test_non_json_bodies_are_recorded_as_unsupported_not_dropped() -> None:
    document = _spec({"/export": {"get": {"responses": {"200": {
        "description": "csv", "content": {"text/csv": {"schema": {"type": "string"}}}}}}}})
    item = parse_spec(document).endpoints[0]
    assert facts(item, "unsupported_content") == {
        "response[200]": {"media_types": ["text/csv"]}}
    assert facts(item, "status_code")["200"]["has_schema"] is False


def test_operation_level_parameter_overrides_path_level() -> None:
    document = _spec({"/items/{id}": {
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"}}],
        "get": {"parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}}}}})
    assert facts(parse_spec(document).endpoints[0], "parameter")["path:id"]["type"] == "integer"


def test_document_without_operations_reports_it() -> None:
    assert "no operations found" in parse_spec(_spec({})).warnings
    assert parse_spec({"openapi": "3.0.3", "paths": "nope"}).endpoints == []


def test_hostile_values_do_not_crash_the_parser() -> None:
    document = _spec({"/x": {"get": {"operationId": {"not": "a string"}, "parameters": "nope",
                                     "responses": {"200": "nope", "default": None}},
                             "post": "not an operation"},
                      "relative": {"get": {"responses": {}}},
                      "/y": None})
    spec = parse_spec(document)
    assert [(e.method, e.path) for e in spec.endpoints] == [("GET", "/x")]
    assert spec.endpoints[0].operation_id is None


# ============================================================================= Swagger 2.0

SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "Old API", "version": "0.9"},
    "host": "api.old.example", "basePath": "/api/", "schemes": ["https"],
    "securityDefinitions": {"key": {"type": "apiKey", "in": "query", "name": "api_key"}},
    "security": [{"key": []}],
    "paths": {"/orders": {
        "post": {
            "parameters": [{"in": "body", "name": "body",
                            "schema": {"$ref": "#/definitions/Order"}},
                           {"in": "query", "name": "limit", "type": "integer", "required": False}],
            "responses": {"201": {"description": "created",
                                  "schema": {"$ref": "#/definitions/Order"}},
                          "400": {"description": "bad"}}},
        "get": {"security": [], "responses": {"200": {"description": "ok"}}}}},
    "definitions": {"Order": {"type": "object", "required": ["sku"],
                              "properties": {"sku": {"type": "string"},
                                             "qty": {"type": "integer", "default": 1}}}},
}


def test_swagger_2_documents_are_parsed() -> None:
    spec = parse_spec(SWAGGER)
    assert spec.spec_format == "swagger" and spec.servers == ["https://api.old.example/api/"]
    post = endpoint(spec, "POST", "/api/orders")
    fields = facts(post, "field")
    assert fields["request:$.sku"] == {"type": "string", "required": True}
    assert fields["request:$.qty"] == {"type": "integer", "required": False, "default": 1}
    assert fields["response[201]:$.sku"]["type"] == "string"
    assert facts(post, "auth") == {"key": {"type": "apiKey", "in": "query", "name": "api_key"}}
    assert facts(post, "pagination")["limit"]["role"] == "limit"
    assert facts(endpoint(spec, "GET", "/api/orders"), "auth") == {"none": {"type": "none"}}


# ============================================================================= fixtures


def fixture(**overrides) -> dict:
    base = {"name": "example",
            "request": {"method": "get", "path": "/v1/charges/1",
                        "headers": {"X-API-Key": "live-key-123"}},
            "response": {"status": 200, "body": {"id": "1"}}}
    base.update(overrides)
    return base


def file_of(*fixtures: dict) -> dict:
    return {"revenant_fixtures": "1.0", "fixtures": list(fixtures)}


def test_demo_fixture_files_parse_completely() -> None:
    legacy = parse_fixture_file(
        json.loads((DEMO / "gravepay" / "fixtures.json").read_text("utf-8")))
    recordings = parse_fixture_file(
        json.loads((DEMO / "novapay" / "recordings.json").read_text("utf-8")))
    assert len(legacy.fixtures) == 10 and legacy.rejected == []
    assert len(recordings.fixtures) == 10 and recordings.rejected == []
    assert {item.pair_with for item in recordings.fixtures} == {
        item.name for item in legacy.fixtures}


def test_credential_headers_and_query_parameters_are_redacted() -> None:
    parsed = parse_fixture_file(file_of(fixture(request={
        "method": "GET", "path": "/v1/charges",
        "query": {"api_key": "qs-secret-1", "page": 2, "access_token": "qs-secret-2"},
        "headers": {"X-API-Key": "hdr-secret-1", "Authorization": "Bearer hdr-secret-2",
                    "Cookie": "sid=hdr-secret-3", "X-Auth-Token": "hdr-secret-4",
                    "Accept": "application/json"}},
        response={"status": 200, "headers": {"Set-Cookie": "sid=resp-secret-5"}, "body": {}})))
    item = parsed.fixtures[0]
    assert item.request["query"] == {"api_key": "[REDACTED]", "page": 2,
                                     "access_token": "[REDACTED]"}
    assert item.request["headers"]["Accept"] == "application/json"
    assert parsed.redactions == 7
    stored = json.dumps(parsed.redacted_document) + json.dumps(item.request) + json.dumps(
        item.response)
    for secret in ("qs-secret-1", "qs-secret-2", "hdr-secret-1", "hdr-secret-2", "hdr-secret-3",
                   "hdr-secret-4", "resp-secret-5"):
        assert secret not in stored


def test_already_redacted_values_are_not_counted_again() -> None:
    parsed = parse_fixture_file(file_of(fixture(request={
        "method": "GET", "path": "/x", "headers": {"X-API-Key": "[REDACTED]"}})))
    assert parsed.redactions == 0


@pytest.mark.parametrize("name", ["X-API-Key", "authorization", "Cookie", "api_key", "apikey",
                                  "X-Auth-Token", "client_secret", "password", "signature"])
def test_credential_names_are_recognised(name: str) -> None:
    assert is_credential_name(name)


@pytest.mark.parametrize("name", ["Accept", "Content-Type", "page", "limit", "X-Request-Id"])
def test_ordinary_names_are_not_treated_as_credentials(name: str) -> None:
    assert not is_credential_name(name)


def test_invalid_fixtures_are_rejected_individually_with_reasons() -> None:
    parsed = parse_fixture_file(file_of(
        fixture(name="good"),
        fixture(name="good"),
        fixture(name=""),
        "not an object",
        fixture(name="bad method", request={"method": "FETCH", "path": "/x"}),
        fixture(name="bad path", request={"method": "GET", "path": "x?y=1"}),
        fixture(name="bad status", response={"status": 999}),
        fixture(name="bool status", response={"status": True}),
        fixture(name="bad headers", request={"method": "GET", "path": "/x", "headers": []}),
        fixture(name="bad variable", variable_fields="$.id"),
        fixture(name="also good", request={"method": "POST", "path": "/x"}),
    ))
    assert [item.name for item in parsed.fixtures] == ["good", "also good"]
    reasons = " | ".join(item["reason"] for item in parsed.rejected)
    assert len(parsed.rejected) == 9
    for expected in ("duplicate name", "'name' is required", "must be an object",
                     "HTTP method", "must start with '/'", "HTTP status code",
                     "headers must be an object", "variable_fields"):
        assert expected in reasons


@pytest.mark.parametrize("document, message", [
    ([], "must be an object"),
    ({"revenant_fixtures": "9.9", "fixtures": []}, "unsupported fixture format version"),
    ({"revenant_fixtures": "1.0", "fixtures": {}}, "must be a list"),
])
def test_unusable_fixture_files_are_reported(document, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_fixture_file(document)


def test_fixture_content_hash_is_stable_and_sensitive_to_content() -> None:
    first = parse_fixture_file(file_of(fixture())).fixtures[0].content_sha256
    again = parse_fixture_file(file_of(fixture())).fixtures[0].content_sha256
    changed = parse_fixture_file(
        file_of(fixture(response={"status": 200, "body": {"id": "2"}}))).fixtures[0].content_sha256
    assert first == again and first != changed and len(first) == 64


@pytest.mark.parametrize("template, path, expected", [
    ("/v1/charges/{id}", "/v1/charges/P-1", 2),
    ("/v1/charges", "/v1/charges", 2),
    ("/v1/charges/{id}/refund", "/v1/charges/P-1/refund", 3),
    ("/v1/charges/search", "/v1/charges/search", 3),
    ("/v1/charges/{id}", "/v1/charges/search", 2),
    ("/v1/charges/{id}", "/v1/charges", None),
    ("/v1/charges/{id}", "/v1/refunds/1", None),
    ("/v1/charges/{id}", "/v1/charges/1/extra", None),
])
def test_path_templates_match_and_prefer_literal_segments(
    template: str, path: str, expected: int | None
) -> None:
    assert template_score(template, path) == expected
