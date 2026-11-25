"""The interpreter of the Revenant Transformation Language.

A pure function of its inputs: no I/O, no clock, no randomness, no database.
Rules are data and are interpreted by the fixed operations below; nothing in a
rule is evaluated as code (docs/transformation-language.md).
"""

from __future__ import annotations

import base64
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from revenant.transform.model import (
    Const,
    Convert,
    Default,
    Drop,
    Each,
    EnumMap,
    ErrorRule,
    Move,
    Pagination,
    Template,
    When,
)

MAX_ARRAY = 10_000
MAX_STRING = 64 * 1024
MAX_EXPONENT = 30  # numbers beyond 10^30 or below 10^-30 are refused, not expanded
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TOKEN = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING: Any = _Missing()


class TransformError(Exception):
    """A rule could not be applied. Carries no values, only what failed."""

    def __init__(self, code: str, message: str, *, rule_index: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule_index = rule_index


# ---- pointers -------------------------------------------------------------------------


def tokens(pointer: str) -> list[str | int]:
    return [name if name else int(index) for name, index in _TOKEN.findall(pointer)]


def get(value: Any, pointer: str, *, fold_case: bool = False) -> Any:
    """The value at ``pointer``, or ``MISSING``."""
    for token in tokens(pointer):
        if isinstance(token, int):
            if not isinstance(value, list) or token >= len(value):
                return MISSING
            value = value[token]
        elif isinstance(value, dict):
            key = token.lower() if fold_case else token
            if key not in value:
                return MISSING
            value = value[key]
        else:
            return MISSING
    return value


def put(document: Any, pointer: str, value: Any) -> Any:
    """Write ``value`` at ``pointer`` and return the (possibly new) document."""
    path = tokens(pointer)
    if not path:
        return value
    if document is MISSING:
        document = [] if isinstance(path[0], int) else {}
    node = document
    for position, token in enumerate(path):
        last = position == len(path) - 1
        following: Any = value if last else ([] if isinstance(path[position + 1], int) else {})
        if isinstance(token, int):
            if not isinstance(node, list) or token > len(node):
                raise TransformError("target_conflict", f"cannot write index {token} of {pointer}")
            if token == len(node):
                node.append(following)
            elif last:
                node[token] = value
            node = node[token]
        else:
            if not isinstance(node, dict):
                raise TransformError("target_conflict",
                                     f"cannot write {pointer}: a parent is not an object")
            if last:
                node[token] = value
            elif token not in node:
                node[token] = following
            node = node[token]
    return document


def remove(document: Any, pointer: str) -> None:
    path = tokens(pointer)
    if not path:
        return
    parent = get(document, "$" + "".join(
        f"[{t}]" if isinstance(t, int) else f".{t}" for t in path[:-1]))
    last = path[-1]
    if isinstance(parent, dict) and isinstance(last, str):
        parent.pop(last, None)
    elif isinstance(parent, list) and isinstance(last, int) and last < len(parent):
        del parent[last]


def read(scopes: dict[str, Any], path: str) -> Any:
    scope, _, pointer = path.partition(":")
    if scope not in scopes:
        return MISSING
    return get(scopes[scope], pointer, fold_case=scope == "header")


# ---- conversions ------------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _from_decimal(result: Decimal) -> int | float:
    if result and abs(result.adjusted()) > MAX_EXPONENT:
        raise TransformError("conversion_failed", "the number is out of range")
    if result == result.to_integral_value():
        return int(result)
    as_float = float(result)
    if Decimal(repr(as_float)) != result:
        raise TransformError("precision_loss", "the conversion would lose precision")
    return as_float


def _decimal(value: Any) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise InvalidOperation
        if isinstance(value, float):
            # A whole float is taken at its exact value; its shortest printed form
            # can differ from it above 2**53. A fraction is taken as printed.
            result = Decimal(int(value)) if value.is_integer() else Decimal(repr(value))
        else:
            result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise TransformError("conversion_failed", "the value is not a number") from exc
    if not result.is_finite():
        raise TransformError("conversion_failed", "the value is not a finite number")
    if result and abs(result.adjusted()) > MAX_EXPONENT:
        raise TransformError("conversion_failed", "the number is out of range")
    return result


def format_iso(seconds: int) -> str:
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime(ISO_FORMAT)
    except (OverflowError, OSError, ValueError) as exc:
        raise TransformError("conversion_failed", "the number is not a valid time") from exc


def parse_iso(text: Any) -> int:
    if not isinstance(text, str):
        raise TransformError("conversion_failed", "the value is not a timestamp string")
    try:
        return int(datetime.strptime(text, ISO_FORMAT).replace(tzinfo=UTC).timestamp())
    except ValueError as exc:
        raise TransformError("conversion_failed",
                             "the value is not a UTC timestamp of the form "
                             "YYYY-MM-DDTHH:MM:SSZ") from exc


def _whole(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransformError("conversion_failed", "the value is not a whole number")
    return value


def convert(value: Any, kind: str, params: dict[str, Any]) -> Any:
    """Apply one named conversion. Raises ``TransformError`` rather than guess."""
    if kind == "string":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) or _number(value):
            return value if isinstance(value, str) else json.dumps(value)
        raise TransformError("conversion_failed", "only scalar values convert to a string")
    if kind == "integer":
        result = _from_decimal(_decimal(value))
        if not isinstance(result, int):
            raise TransformError("precision_loss", "the value is not a whole number")
        return result
    if kind == "number":
        return _from_decimal(_decimal(value))
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise TransformError("conversion_failed", "the value is not a boolean")
    if kind == "epoch_s_to_iso8601":
        return format_iso(_whole(value))
    if kind == "iso8601_to_epoch_s":
        return parse_iso(value)
    if kind == "epoch_ms_to_iso8601":
        if _whole(value) % 1000:
            raise TransformError("precision_loss", "milliseconds would be lost")
        return format_iso(value // 1000)
    if kind == "iso8601_to_epoch_ms":
        return parse_iso(value) * 1000
    if kind == "scale":
        return _from_decimal(_decimal(value) * _decimal(params.get("factor")))
    raise TransformError("unknown_conversion", f"unknown conversion {kind}")


# ---- conditions -------------------------------------------------------------------------


def holds(when: When | None, scopes: dict[str, Any]) -> bool:
    if when is None:
        return True
    if when.exists is not None and read(scopes, when.exists) is MISSING:
        return False
    if when.equals is not None and not _same(read(scopes, when.equals[0]), when.equals[1]):
        return False
    if when.in_ is not None and not any(
            _same(read(scopes, when.in_[0]), item) for item in when.in_[1]):
        return False
    if when.status_in is not None and scopes.get("status") not in when.status_in:
        return False
    return not (when.not_ is not None and holds(when.not_, scopes))


def _same(left: Any, right: Any) -> bool:
    if left is MISSING:
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return bool(left == right)


# ---- rules ------------------------------------------------------------------------------


@dataclass
class Applied:
    output: Any
    trace: list[dict[str, Any]] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)  # generalised source paths that were read


def _generalise(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def apply_rules(rules: list[Any], scopes: dict[str, Any], *, start: Any = MISSING,
                item_prefix: str | None = None) -> Applied:
    """Run ``rules`` in document order. ``start`` is the output to begin from
    (``MISSING`` in build mode, a copy of the source in passthrough mode)."""
    result = Applied(output=start)

    def note(source: str) -> None:
        if item_prefix is not None and source.startswith("item:"):
            result.consumed.add(_generalise(item_prefix + "[]" + source[6:]))
        else:
            result.consumed.add(_generalise(source))

    for index, rule in enumerate(rules):
        entry: dict[str, Any] = {
            "rule_index": index, "op": rule.op, "mapping_id": rule.mapping_id,
            "from": getattr(rule, "from_", None),
            "to": getattr(rule, "to", None) or getattr(rule, "path", None)}
        result.trace.append(entry)
        try:
            if not holds(rule.when, scopes):
                entry["outcome"] = "skipped_condition"
                continue
            entry["outcome"] = _apply(rule, scopes, result, note)
        except TransformError as exc:
            entry["outcome"] = "error"
            exc.rule_index = index if exc.rule_index is None else exc.rule_index
            raise
    return result


def _source(rule: Any, scopes: dict[str, Any], note: Any) -> Any:
    value = read(scopes, rule.from_)
    if value is MISSING:
        if rule.required:
            raise TransformError("missing_required",
                                 f"required source {rule.from_} is missing")
        return MISSING
    note(rule.from_)
    return value


def _check(value: Any) -> Any:
    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_STRING:
        raise TransformError("limit_exceeded", "a string value exceeds the size limit")
    return value


def _apply(rule: Any, scopes: dict[str, Any], result: Applied, note: Any) -> str:
    if isinstance(rule, Const):
        result.output = put(result.output, rule.to, copy.deepcopy(rule.value))
    elif isinstance(rule, Default):
        if get(result.output, rule.to) is not MISSING:
            return "skipped_condition"
        result.output = put(result.output, rule.to, copy.deepcopy(rule.value))
    elif isinstance(rule, Move):
        value = _source(rule, scopes, note)
        if value is MISSING:
            return "skipped_missing"
        result.output = put(result.output, rule.to, copy.deepcopy(_check(value)))
    elif isinstance(rule, Convert):
        value = _source(rule, scopes, note)
        if value is MISSING:
            return "skipped_missing"
        result.output = put(result.output, rule.to, convert(value, rule.as_, rule.params))
    elif isinstance(rule, EnumMap):
        value = _source(rule, scopes, note)
        if value is MISSING:
            return "skipped_missing"
        key = value if isinstance(value, str) else json.dumps(value)
        if key in rule.map:
            mapped = copy.deepcopy(rule.map[key])
        elif rule.on_unmapped == "error":
            raise TransformError("unmapped_enum_value", f"no mapping for a value at {rule.from_}")
        elif rule.on_unmapped == "passthrough":
            mapped = value
        else:
            mapped = copy.deepcopy(rule.on_unmapped.default)
        result.output = put(result.output, rule.to, mapped)
    elif isinstance(rule, Template):
        values: dict[str, str] = {}
        for name, path in rule.vars.items():
            value = read(scopes, path)
            if value is MISSING:
                return "skipped_missing"
            if value is None or isinstance(value, dict | list):
                raise TransformError("conversion_failed",
                                     f"template variable {name} is not a scalar")
            note(path)
            values[name] = convert(value, "string", {})
        # One pass over the template: substituted text is never scanned again.
        text = _PLACEHOLDER.sub(lambda match: values[match.group(1)], rule.template)
        result.output = put(result.output, rule.to, _check(text))
    elif isinstance(rule, Each):
        items = read(scopes, rule.from_)
        if items is MISSING:
            return "skipped_missing"
        if not isinstance(items, list):
            raise TransformError("not_an_array", f"{rule.from_} is not an array")
        if len(items) > MAX_ARRAY:
            raise TransformError("limit_exceeded", f"{rule.from_} has too many elements")
        produced = []
        for item in items:
            inner = apply_rules(rule.rules, {**scopes, "item": item},
                                item_prefix=rule.from_)
            result.consumed |= inner.consumed
            produced.append({} if inner.output is MISSING else inner.output)
        result.output = put(result.output, rule.to, produced)
    elif isinstance(rule, Drop):
        remove(result.output, rule.path)
    else:  # pragma: no cover - the rule union is closed
        raise TransformError("unknown_rule", "unknown rule")
    return "applied"


def unmapped_fields(body: Any, consumed: set[str]) -> list[str]:
    """Leaves of the source body that no rule read (build mode)."""
    found: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for value in node:
                walk(value, f"{path}[]")
        else:
            found.add(path)

    if body is not None and body is not MISSING:
        walk(body, "body:$")
    covered = {path for path in consumed if path.startswith("body:")}
    return sorted(path for path in found if not any(
        path == item or path.startswith((item + ".", item + "[")) for item in covered))


# ---- pagination -------------------------------------------------------------------------


def encode_cursor(pagination: Pagination, page: int, limit: int | None) -> str:
    first = pagination.legacy.first_page
    values = {"$page": page, "$limit": limit,
              "$offset": (page - first) * limit if limit is not None else None}
    cursor = pagination.cursor
    if cursor is None:  # pragma: no cover - the model requires it for computed_cursor
        raise TransformError("pagination", "no cursor definition")

    def resolve(placeholder: str) -> int:
        value = values[placeholder]
        if value is None:
            raise TransformError("invalid_pagination", "the page size is needed for the cursor")
        return value

    if cursor.encoding == "plain":
        return str(resolve(cursor.shape))
    shape = {key: resolve(value) for key, value in cursor.shape.items()}
    return base64.b64encode(
        json.dumps(shape, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TransformError("invalid_pagination", f"{name} must be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise TransformError("invalid_pagination", f"{name} must be a whole number")


def paging_request(pagination: Pagination, query: dict[str, Any]) -> tuple[int, int | None,
                                                                         dict[str, Any]]:
    """Legacy page and limit, and the upstream query parameters that express them."""
    legacy = pagination.legacy
    raw_page = query.get(legacy.page_param, MISSING)
    page = legacy.first_page if raw_page is MISSING else _positive(raw_page, legacy.page_param)
    raw_limit = query.get(legacy.limit_param, MISSING)
    limit = legacy.default_limit if raw_limit is MISSING else _positive(
        raw_limit, legacy.limit_param)
    if page < legacy.first_page:
        raise TransformError("invalid_pagination",
                             f"{legacy.page_param} must be at least {legacy.first_page}")
    if limit is not None and (limit < 1 or (legacy.max_limit and limit > legacy.max_limit)):
        raise TransformError("invalid_pagination", f"{legacy.limit_param} is out of range")
    if pagination.strategy != "computed_cursor":
        raise TransformError("unsupported_pagination",
                             f"the {pagination.strategy} strategy is not available")
    upstream: dict[str, Any] = {
        pagination.upstream.cursor_param: encode_cursor(pagination, page, limit)}
    if pagination.upstream.size_param and limit is not None:
        upstream[pagination.upstream.size_param] = limit
    return page, limit, upstream


# ---- whole messages ---------------------------------------------------------------------


@dataclass
class UpstreamRequest:
    method: str
    path: str
    query: dict[str, Any]
    headers: dict[str, str]
    body: Any
    trace: dict[str, list[dict[str, Any]]]
    unmapped_fields: list[str]


@dataclass
class LegacyResponse:
    status: int
    headers: dict[str, str]
    body: Any
    trace: dict[str, list[dict[str, Any]]]
    unmapped_error: bool = False
    unmapped_status: bool = False
    unmapped_fields: list[str] = field(default_factory=list)


def match_path(template: str, path: str) -> dict[str, str] | None:
    expected, actual = template.strip("/").split("/"), path.strip("/").split("/")
    if len(expected) != len(actual):
        return None
    found: dict[str, str] = {}
    for part, value in zip(expected, actual, strict=True):
        if part.startswith("{") and part.endswith("}"):
            if not value:
                return None
            found[part[1:-1]] = value
        elif part != value:
            return None
    return found


def request_scopes(route: Any, path: str, query: dict[str, Any], headers: dict[str, Any],
                   body: Any) -> dict[str, Any]:
    params = match_path(route.match.path, path)
    if params is None:
        raise TransformError("no_route", "the path does not match the route")
    return {"body": body, "query": dict(query), "path": params,
            "header": {str(name).lower(): value for name, value in headers.items()}}


def _scalar_map(output: Any, what: str) -> dict[str, Any]:
    if output is MISSING:
        return {}
    if not isinstance(output, dict) or any(
            isinstance(value, dict | list) or value is None for value in output.values()):
        raise TransformError("invalid_output", f"{what} must be simple values")
    return output


def transform_request(route: Any, scopes: dict[str, Any]) -> UpstreamRequest:
    """Legacy request scopes in, upstream request out. Nothing is sent."""
    section = route.request
    path_values = apply_rules(section.path, scopes)
    values = _scalar_map(path_values.output, "path parameters")

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise TransformError("missing_required", f"no value for path parameter {name}")
        # Encoded as one segment: a value can never add a segment or a query string.
        return quote(convert(values[name], "string", {}), safe="")

    path = _PLACEHOLDER.sub(substitute, route.upstream.path)
    query = apply_rules(section.query, scopes)
    upstream_query = dict(_scalar_map(query.output, "query parameters"))
    if route.pagination is not None:
        _page, _limit, extra = paging_request(route.pagination, scopes["query"])
        upstream_query.update(extra)
    headers = apply_rules(section.headers, scopes)
    start = copy.deepcopy(scopes["body"]) if section.body.mode == "passthrough" else MISSING
    body = apply_rules(section.body.rules, scopes, start=start)
    consumed = path_values.consumed | query.consumed | headers.consumed | body.consumed
    return UpstreamRequest(
        method=route.upstream.method, path=path, query=upstream_query,
        headers={name: convert(value, "string", {})
                 for name, value in _scalar_map(headers.output, "headers").items()},
        body=None if body.output is MISSING else body.output,
        trace={"path": path_values.trace, "query": query.trace, "headers": headers.trace,
               "body": body.trace},
        unmapped_fields=[] if section.body.mode == "passthrough" else unmapped_fields(
            scopes["body"], consumed))


def _error_response(envelope: list[Any], errors: list[ErrorRule], status: int, body: Any,
                    scopes: dict[str, Any]) -> LegacyResponse:
    for index, rule in enumerate(errors):
        if not holds(rule.when, scopes):
            continue
        variables: dict[str, Any] = {}
        for name, var in rule.vars.items():
            if var.from_ is None:
                variables[name] = copy.deepcopy(var.const)
            elif (value := read(scopes, var.from_)) is not MISSING:
                variables[name] = value
        built = apply_rules(envelope, {"var": variables})
        return LegacyResponse(
            status=rule.status, headers={},
            body=None if built.output is MISSING else built.output,
            trace={"errors": [{"rule_index": index, "mapping_id": rule.mapping_id,
                               "outcome": "applied"}], "envelope": built.trace})
    # No approved rule matches: keep the status and invent nothing.
    return LegacyResponse(status=status, headers={}, body=None,
                          trace={"errors": [], "envelope": []}, unmapped_error=True)


def transform_response(route: Any, envelope: list[Any], status: int, body: Any,
                       request: dict[str, Any]) -> LegacyResponse:
    """Upstream response in, legacy response out. ``request`` are the legacy
    request scopes, readable as ``request.body``, ``request.query``, ``request.path``."""
    scopes = {"body": body, "status": status, "request.body": request.get("body"),
              "request.query": request.get("query", {}), "request.path": request.get("path", {})}
    section = route.response
    if status >= 400:
        return _error_response(envelope, section.errors, status, body, scopes)
    mapped = next((item.to for item in section.status_map if item.from_ == status), None)
    start = copy.deepcopy(body) if section.body.mode == "passthrough" else MISSING
    built = apply_rules(section.body.rules, scopes, start=start)
    output = built.output
    consumed = set(built.consumed)
    paging = route.pagination
    if paging is not None:
        page, limit, _ = paging_request(paging, scopes["request.query"])
        response = paging.response
        if get(output, response.items_to) is MISSING:
            items = read(scopes, response.items_from)
            if items is not MISSING and not isinstance(items, list):
                raise TransformError("not_an_array", f"{response.items_from} is not an array")
            if items == []:
                output = put(output, response.items_to, [])
        consumed.add(_generalise(response.items_from))
        if response.has_more_to and response.has_more_from_presence_of:
            marker = read(scopes, response.has_more_from_presence_of)
            output = put(output, response.has_more_to, marker is not MISSING and marker is not None)
            consumed.add(_generalise(response.has_more_from_presence_of))
        for target, placeholder in sorted(response.echo.items()):
            value = page if placeholder == "$page" else limit
            if value is not None:
                output = put(output, target, value)
    headers = apply_rules(section.headers, scopes)
    return LegacyResponse(
        status=status if mapped is None else mapped,
        headers={name: convert(value, "string", {})
                 for name, value in _scalar_map(headers.output, "headers").items()},
        body=None if output is MISSING else output,
        trace={"body": built.trace, "headers": headers.trace},
        unmapped_status=mapped is None,
        unmapped_fields=[] if section.body.mode == "passthrough" else unmapped_fields(
            body, consumed | headers.consumed))
