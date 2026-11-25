"""Static validation of an Adapter Document (docs/transformation-language.md, section 8).

Run when a document is compiled and again when it is loaded. Every failure is
listed; a document with any failure is rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from revenant.adapter.document import MAX_DOCUMENT_BYTES, AdapterDocument, Route
from revenant.transform.model import (
    CREDENTIAL_HEADERS,
    HTTP_METHODS,
    REQUEST_SCOPES,
    RESPONSE_SCOPES,
    SAFE_METHODS,
    Each,
    check_pointer,
    check_rule,
    split_path,
)


@dataclass
class Context:
    """What the validator may check a document against. All parts are optional,
    so a stored document can be re-validated at load time without the database."""

    approved_mapping_ids: set[str] | None = None
    # route id -> pointers that the upstream request body must contain
    required_request_fields: dict[str, set[str]] = field(default_factory=dict)
    base_url_problem: str | None = None
    allow_unauthenticated: bool = False


def _template_params(path: str) -> list[str]:
    return [part[1:-1] for part in path.strip("/").split("/")
            if part.startswith("{") and part.endswith("}")]


def carriers(document: AdapterDocument) -> tuple[set[str], set[str], set[str], set[str]]:
    """Credential carriers, lower case: (headers read, query read, headers written,
    query written) that no rule may touch."""
    read_headers, written_headers = set(CREDENTIAL_HEADERS), set(CREDENTIAL_HEADERS)
    read_query: set[str] = set()
    written_query: set[str] = set()
    inbound, outbound = document.auth.inbound, document.auth.outbound
    if inbound.type == "api_key_header" and inbound.header:
        read_headers.add(inbound.header.lower())
        written_headers.add(inbound.header.lower())
    if inbound.type == "api_key_query" and inbound.param:
        read_query.add(inbound.param.lower())
    if outbound.type == "api_key_header" and outbound.header:
        written_headers.add(outbound.header.lower())
        read_headers.add(outbound.header.lower())
    if outbound.type == "api_key_query" and outbound.param:
        written_query.add(outbound.param.lower())
    return read_headers, read_query, written_headers, written_query


def _targets(rules: list[Any], prefix: str = "") -> list[tuple[str, bool]]:
    """(generalised target pointer, unconditional) for every rule, including nested ones."""
    found: list[tuple[str, bool]] = []
    for rule in rules:
        for target in rule.targets():
            found.append((prefix + target, rule.when is None and rule.op != "default"
                          and rule.op != "drop"))
        if isinstance(rule, Each):
            found += [(f"{prefix}{rule.to}[]{target[1:]}", unconditional and rule.when is None)
                      for target, unconditional in _targets(rule.rules)]
    return found


def _all_rules(rules: list[Any]) -> list[Any]:
    found: list[Any] = []
    for rule in rules:
        found.append(rule)
        if isinstance(rule, Each):
            found += _all_rules(rule.rules)
    return found


class _Checker:
    def __init__(self, document: AdapterDocument, context: Context) -> None:
        self.document = document
        self.context = context
        self.problems: list[str] = []
        self.read_headers, self.read_query, self.written_headers, self.written_query = carriers(
            document)

    def fail(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def mapping(self, where: str, mapping_id: str | None) -> None:
        if not mapping_id:
            self.fail(where, "no mapping_id; every rule must come from an approved mapping")
        elif (self.context.approved_mapping_ids is not None
              and mapping_id not in self.context.approved_mapping_ids):
            self.fail(where, f"mapping {mapping_id} is not an approved mapping")

    def rules(self, where: str, rules: list[Any], scopes: frozenset[str],
              target_scope: str | None) -> None:
        for index, rule in enumerate(rules):
            for problem in check_rule(rule, scopes, target_scope=target_scope):
                self.fail(f"{where}[{index}]", problem)
        for rule in _all_rules(rules):
            self.mapping(f"{where} ({rule.op})", rule.mapping_id)
            for source in rule.sources():
                scope, _, pointer = source.partition(":")
                name = pointer[2:].split(".")[0].split("[")[0].lower()
                if scope == "header" and name in self.read_headers:
                    self.fail(where, f"{source} is a credential carrier and cannot be read")
                if scope in ("query", "request.query") and name in self.read_query:
                    self.fail(where, f"{source} is a credential carrier and cannot be read")
            for target in rule.targets():
                name = target[2:].split(".")[0].split("[")[0].lower()
                if target_scope == "header" and name in self.written_headers:
                    self.fail(where, f"header {name} is a credential carrier and cannot be "
                                     "written")
                if target_scope == "query" and name in self.written_query | self.read_query:
                    self.fail(where, f"query parameter {name} is a credential carrier and "
                                     "cannot be written")
        seen: set[str] = set()
        for target, unconditional in _targets(rules):
            if unconditional:
                if target in seen:
                    self.fail(where, f"two unconditional rules write {target}")
                seen.add(target)

    def route(self, route: Route) -> None:
        where = f"route {route.id}"
        for side, value in (("match", route.match), ("upstream", route.upstream)):
            if value.method not in HTTP_METHODS:
                self.fail(where, f"{side}.method {value.method!r} is not an HTTP method")
        self.mapping(where, route.mapping_id)
        unsafe = [value.method for value in (route.match, route.upstream)
                  if value.method not in SAFE_METHODS]
        if route.effect == "read_only":
            if unsafe:
                self.fail(where, "declared read_only but uses " + " and ".join(unsafe)
                          + ", which is not GET, HEAD or OPTIONS")
            self.mapping(f"{where} effect", route.effect_mapping_id)
        request, response = frozenset(REQUEST_SCOPES), frozenset(RESPONSE_SCOPES)
        self.rules(f"{where} request.path", route.request.path, request, "path")
        self.rules(f"{where} request.query", route.request.query, request, "query")
        self.rules(f"{where} request.headers", route.request.headers, request, "header")
        self.rules(f"{where} request.body", route.request.body.rules, request, None)
        self.rules(f"{where} response.headers", route.response.headers, response, "header")
        self.rules(f"{where} response.body", route.response.body.rules, response, None)
        produced = {target[2:] for target, _ in _targets(route.request.path)}
        for name in _template_params(route.upstream.path):
            if name not in produced:
                self.fail(where, f"no rule produces the path parameter {{{name}}}")
        match_params = set(_template_params(route.match.path))
        for rule in _all_rules(route.request.path + route.request.query + route.request.headers
                               + route.request.body.rules):
            for source in rule.sources():
                if source.startswith("path:") and source[7:] not in match_params:
                    self.fail(where, f"{source} is not a parameter of the legacy path")
        for index, item in enumerate(route.response.status_map):
            self.mapping(f"{where} status_map[{index}]", item.mapping_id)
        if len({item.from_ for item in route.response.status_map}) != len(
                route.response.status_map):
            self.fail(where, "status_map maps one upstream status twice")
        for index, error in enumerate(route.response.errors):
            self.mapping(f"{where} errors[{index}]", error.mapping_id)
            sources = [*error.when.paths(),
                       *(var.from_ for var in error.vars.values() if var.from_)]
            for source in sources:
                try:
                    scope, _ = split_path(source)
                except ValueError as exc:
                    self.fail(f"{where} errors[{index}]", str(exc))
                    continue
                if scope not in RESPONSE_SCOPES:
                    self.fail(f"{where} errors[{index}]", f"scope {scope!r} cannot be read")
        self.pagination(where, route)
        needed = self.context.required_request_fields.get(route.id, set())
        written = {target for target, _ in _targets(route.request.body.rules)}
        missing = sorted(pointer for pointer in needed if not any(
            pointer == item or pointer.startswith((item + ".", item + "["))
            for item in written))
        if missing and route.request.body.mode == "build":
            self.fail(where, "required replacement request fields are not produced: "
                      + ", ".join(missing))

    def pagination(self, where: str, route: Route) -> None:
        paging = route.pagination
        if paging is None:
            return
        self.mapping(f"{where} pagination", paging.mapping_id)
        for name in (paging.legacy.page_param, paging.legacy.limit_param):
            if name.lower() in self.read_query:
                self.fail(where, f"pagination reads the credential carrier {name}")
        for written in (paging.upstream.cursor_param, paging.upstream.size_param):
            if written and written.lower() in self.written_query | self.read_query:
                self.fail(where, f"pagination writes the credential carrier {written}")
        response = paging.response
        for label, source in (("items_from", response.items_from),
                              ("has_more_from_presence_of", response.has_more_from_presence_of)):
            if source is None:
                continue
            try:
                scope, _ = split_path(source)
                if scope != "body":
                    self.fail(where, f"pagination {label} must read the body")
            except ValueError as exc:
                self.fail(where, f"pagination {label}: {exc}")
        for label, pointer in (("items_to", response.items_to),
                               ("has_more_to", response.has_more_to), *(
                                   ("echo", key) for key in response.echo)):
            if pointer is None:
                continue
            try:
                check_pointer(pointer)
            except ValueError as exc:
                self.fail(where, f"pagination {label}: {exc}")
        if paging.strategy != "computed_cursor":
            self.fail(where, f"pagination strategy {paging.strategy} is not available in this "
                             "release")

    def run(self) -> list[str]:
        document, context = self.document, self.context
        if context.base_url_problem:
            self.fail("upstream.base_url", context.base_url_problem)
        if not document.upstream.base_url.startswith(("http://", "https://")):
            self.fail("upstream.base_url", "must be an http or https URL")
        self.mapping("auth", document.auth.mapping_id)
        if document.auth.inbound.type == "none" and not context.allow_unauthenticated:
            self.fail("auth.inbound", "an unauthenticated runtime needs the project setting "
                                      "allow_unauthenticated_runtime")
        if document.error_envelope.rules:
            self.mapping("error_envelope", document.error_envelope.mapping_id)
        for index, rule in enumerate(document.error_envelope.rules):
            for problem in check_rule(rule, frozenset({"var"})):
                self.fail(f"error_envelope[{index}]", problem)
        ids = [route.id for route in document.routes]
        for name in sorted({item for item in ids if ids.count(item) > 1}):
            self.fail("routes", f"route id {name} is used twice")
        matches = [(route.match.method, route.match.path) for route in document.routes]
        for method, path in sorted({item for item in matches if matches.count(item) > 1}):
            self.fail("routes", f"{method} {path} is matched by two routes")
        for route in document.routes:
            self.route(route)
        return self.problems


def validate(data: Any, context: Context | None = None) -> tuple[AdapterDocument | None,
                                                                list[str]]:
    """Returns the parsed document (if it has the right shape) and every failure."""
    if not isinstance(data, dict):
        return None, ["document: must be an object"]
    if len(json.dumps(data).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        return None, [f"document: larger than {MAX_DOCUMENT_BYTES} bytes"]
    try:
        document = AdapterDocument.model_validate(data)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in error['loc']) or 'document'}: {error['msg']}"
                      for error in exc.errors()]
    return document, _Checker(document, context or Context()).run()
