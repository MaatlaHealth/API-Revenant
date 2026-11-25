"""Validation of a mapping's transformation when a person edits it.

An edited mapping is checked against the same rule models the adapter will be
built from, so nothing that is not a rule of the language can be stored
(requirement 4.2). Credential carriers cannot be read or written by any rule.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from revenant.transform.model import (
    CREDENTIAL_HEADERS,
    HTTP_METHODS,
    REQUEST_SCOPES,
    RESPONSE_SCOPES,
    AuthBlock,
    ErrorRule,
    Pagination,
    Rule,
    check_pointer,
    check_rule,
    is_name,
    split_path,
)

FIELD_CATEGORIES = ("request_field", "response_field", "query", "header", "enum", "type")
REQUEST_SECTIONS = ("request.body", "request.query", "request.headers")
RESPONSE_SECTIONS = ("response.body", "response.headers")
_RULE: TypeAdapter[Any] = TypeAdapter(Rule)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _Each(_Model):
    from_: str = Field(alias="from")
    to: str


class _FieldShape(_Model):
    section: Literal["request.body", "request.query", "request.headers", "response.body",
                     "response.headers"]
    rule: dict[str, Any]
    each: _Each | None = None


class _Side(_Model):
    method: str
    path: str = Field(pattern=r"^/", max_length=500)


class _PathParam(_Model):
    from_: str = Field(alias="from")
    to: str


class _Route(_Model):
    match: _Side
    upstream: _Side
    path_params: list[_PathParam] = Field(default_factory=list)


class _Status(_Model):
    section: Literal["response.status"]
    from_: int = Field(alias="from", ge=100, le=599)
    to: int = Field(ge=100, le=599)


class _Envelope(_Model):
    kind: Literal["error_envelope"]
    rules: list[dict[str, Any]] = Field(max_length=50)


def _messages(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"
            for error in exc.errors()]


def _template_params(path: str) -> list[str]:
    return [part[1:-1] for part in path.strip("/").split("/")
            if part.startswith("{") and part.endswith("}")]


def _field(data: dict[str, Any]) -> list[str]:
    shape = _FieldShape.model_validate(data)
    rule = _RULE.validate_python(shape.rule)
    request = shape.section in REQUEST_SECTIONS
    scopes = set(REQUEST_SCOPES if request else RESPONSE_SCOPES)
    problems: list[str] = []
    if shape.each is not None:
        scopes.add("item")
        try:
            scope, _ = split_path(shape.each.from_)
            check_pointer(shape.each.to)
            if scope not in (REQUEST_SCOPES if request else RESPONSE_SCOPES):
                problems.append(f"each.from: scope {scope!r} cannot be read here")
        except ValueError as exc:
            problems.append(f"each: {exc}")
    target_scope = {"request.headers": "header", "response.headers": "header",
                    "request.query": "query"}.get(shape.section)
    return problems + check_rule(rule, frozenset(scopes), target_scope=target_scope)


def _route(data: dict[str, Any]) -> list[str]:
    route = _Route.model_validate(data)
    problems = [f"{side}.method: not an HTTP method"
                for side, value in (("match", route.match), ("upstream", route.upstream))
                if value.method not in HTTP_METHODS]
    wanted = _template_params(route.upstream.path)
    given = [item.to for item in route.path_params]
    for name in wanted:
        if given.count(name) != 1:
            problems.append(f"path_params: exactly one source is needed for {{{name}}}")
    problems += [f"path_params: {{{name}}} is not in the upstream path"
                 for name in given if name not in wanted]
    for item in route.path_params:
        try:
            scope, pointer = split_path(item.from_)
        except ValueError as exc:
            problems.append(f"path_params: {exc}")
            continue
        if scope not in REQUEST_SCOPES:
            problems.append(f"path_params: scope {scope!r} cannot be read here")
        if scope == "header" and pointer[2:].lower() in CREDENTIAL_HEADERS:
            problems.append(f"path_params: {item.from_} is a credential carrier")
        if scope == "path" and pointer[2:] not in _template_params(route.match.path):
            problems.append(f"path_params: {item.from_} is not a parameter of the legacy path")
    return problems


def _error(data: dict[str, Any]) -> list[str]:
    if data.get("kind") == "error_envelope":
        envelope = _Envelope.model_validate(data)
        problems: list[str] = []
        for raw in envelope.rules:
            problems += check_rule(_RULE.validate_python(raw), frozenset({"var"}))
        return problems
    if data.get("kind") != "error_rule":
        return ["kind: must be error_rule or error_envelope"]
    rule = ErrorRule.model_validate({k: v for k, v in data.items() if k != "kind"})
    problems = []
    sources = [*rule.when.paths(), *(var.from_ for var in rule.vars.values() if var.from_)]
    for source in sources:
        try:
            scope, _ = split_path(source)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if scope not in RESPONSE_SCOPES:
            problems.append(f"scope {scope!r} cannot be read by an error rule")
    problems += [f"vars: {name!r} is not a plain name" for name in rule.vars if not is_name(name)]
    return problems


def validate_transformation(category: str, data: Any) -> list[str]:
    """Every problem with an edited transformation; empty when it is acceptable."""
    if not isinstance(data, dict):
        return ["the transformation must be an object"]
    try:
        if category in FIELD_CATEGORIES:
            return _field(data)
        if category == "endpoint":
            return _route(data)
        if category == "status_code":
            _Status.model_validate(data)
            return []
        if category == "error":
            return _error(data)
        if category == "auth":
            AuthBlock.model_validate(data)
            return []
        if category == "pagination":
            Pagination.model_validate(data)
            return []
    except ValidationError as exc:
        return _messages(exc)
    return [f"mappings of category {category} cannot be edited here"]
