"""Pagination detector for one endpoint pair.

Only the ``computed_cursor`` strategy is proposed, and only when paired
recordings show how the replacement's cursor follows from the legacy page
number. Anything the recordings do not show is reported, not assumed.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from revenant.evidence.openapi import PAGINATION_ROLES
from revenant.reconstruction.contract import EndpointContract, FixtureInfo, ParamInfo
from revenant.reconstruction.pairing import Pair
from revenant.reconstruction.types import Ref, dedupe


@dataclass
class PaginationOutcome:
    transformation: dict[str, Any] | None = None
    confidence: str = "inferred"
    rationale: str = ""
    evidence_refs: list[Ref] = field(default_factory=list)
    problem: str | None = None           # why no mapping could be proposed
    termination_established: bool = False
    legacy_query: set[str] = field(default_factory=set)        # consumed scoped paths
    replacement_query: set[str] = field(default_factory=set)
    legacy_response: set[str] = field(default_factory=set)     # produced by pagination


def role_param(endpoint: EndpointContract, role: str) -> ParamInfo | None:
    found = [param for param in endpoint.params if param.location == "query"
             and param.role == role]
    return found[0] if len(found) == 1 else None


def is_paginated(endpoint: EndpointContract) -> bool:
    return role_param(endpoint, "page") is not None


def page_of(fixture: FixtureInfo, param: str) -> int | None:
    value = (fixture.request.get("query") or {}).get(param)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def array_paths(endpoint: EndpointContract) -> list[str]:
    """Top-level arrays of the success response."""
    return sorted(path for path, info in endpoint.success_fields().items()
                  if info.type == "array" and "[]" not in path and path.count(".") == 1)


def termination_fixtures(endpoint: EndpointContract) -> list[FixtureInfo]:
    """Legacy fixtures that show how a list ends: a short page, or a false flag."""
    page, limit = role_param(endpoint, "page"), role_param(endpoint, "limit")
    arrays = array_paths(endpoint)
    found: list[FixtureInfo] = []
    for fixture in endpoint.fixtures:
        body = fixture.response.get("body")
        if page is None or page_of(fixture, page.name) is None or not isinstance(body, dict) \
                or not 200 <= fixture.status < 300:
            continue
        size = page_of(fixture, limit.name) if limit else None
        items = body.get(arrays[0][2:]) if len(arrays) == 1 else None
        short = isinstance(items, list) and size is not None and len(items) < size
        flagged = any(value is False for value in body.values())
        if short or flagged:
            found.append(fixture)
    return found


def _placeholders(value: Any, page: int, limit: int | None, first: int) -> set[str]:
    found: set[str] = set()
    for candidate in (value, int(value) if isinstance(value, str) and value.isdigit() else None):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            continue
        if candidate == page:
            found.add("$page")
        if limit is not None and candidate == limit:
            found.add("$limit")
        if limit is not None and candidate == (page - first) * limit:
            found.add("$offset")
    return found


def _decode(cursor: Any) -> tuple[str, Any] | None:
    """``(encoding, decoded value)`` for a recorded cursor, if it has a known form."""
    if not isinstance(cursor, str) or not cursor:
        return None
    if cursor.isdigit():
        return "plain", cursor
    try:
        decoded = json.loads(base64.b64decode(cursor, validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return ("base64_json", decoded) if isinstance(decoded, dict) and decoded else None


def detect(pair: Pair) -> PaginationOutcome | None:
    """None when the legacy endpoint is not paginated by page number."""
    legacy, replacement = pair.legacy, pair.replacement
    page, limit = role_param(legacy, "page"), role_param(legacy, "limit")
    if page is None:
        return None
    out = PaginationOutcome()
    cursor, size = role_param(replacement, "cursor"), role_param(replacement, "limit")
    if cursor is None:
        if role_param(replacement, "page") is not None:
            return None  # page-numbered on both sides: ordinary query mappings apply
        out.problem = ("the legacy endpoint is paginated by page number, but the replacement "
                       "declares no cursor parameter")
        return out
    first = page.minimum if isinstance(page.minimum, int) else (
        page.default if page.has_default and isinstance(page.default, int) else 1)
    first_declared = isinstance(page.minimum, int) or (
        page.has_default and isinstance(page.default, int))
    refs: list[Ref] = [page.ref, cursor.ref] + ([limit.ref] if limit else []) + (
        [size.ref] if size else [])

    # ---- how the cursor follows from the page number ---------------------------------
    shape: Any = None
    encoding: str | None = None
    pages: set[int] = set()
    consistent = True
    size_confirmed, size_contradicted = 0, False
    observed = [(lf, rf, page_of(lf, page.name)) for lf, rf in pair.fixture_pairs]
    for lf, rf, number in observed:
        if number is None or not 200 <= lf.status < 300:
            continue
        refs += [lf.ref, rf.ref]
        requested = page_of(lf, limit.name) if limit else None
        query = rf.request.get("query") or {}
        if size and requested is not None and size.name in query:
            if page_of(rf, size.name) == requested:
                size_confirmed += 1
            else:
                size_contradicted = True
        decoded = _decode(query.get(cursor.name))
        if decoded is None:
            if cursor.name in query:
                consistent = False
            continue
        kind, value = decoded
        if kind == "plain":
            options: Any = _placeholders(value, number, requested, first)
        else:
            options = {key: _placeholders(item, number, requested, first)
                       for key, item in value.items()}
        if encoding is None:
            encoding, shape = kind, options
        elif kind != encoding or (kind == "base64_json" and set(options) != set(shape)):
            consistent = False
        elif kind == "plain":
            shape = shape & options
        else:
            shape = {key: shape[key] & options[key] for key in shape}
        pages.add(number)

    def settled(options: Any) -> bool:
        return isinstance(options, set) and len(options) == 1

    if encoding is None or not consistent or not (
            settled(shape) if encoding == "plain" else all(settled(v) for v in shape.values())):
        out.problem = ("no paired recordings establish how the replacement cursor is derived "
                       "from the legacy page number")
        out.evidence_refs = dedupe(refs)
        return out
    cursor_shape = next(iter(shape)) if encoding == "plain" else {
        key: next(iter(value)) for key, value in sorted(shape.items())}
    notes = [f"The replacement cursor is the {encoding} form of {json.dumps(cursor_shape)}, "
             f"confirmed by recordings of {len(pages)} page{'s' if len(pages) != 1 else ''}."]
    all_observed = len(pages) >= 2
    if not first_declared:
        all_observed = False
        notes.append("The first page number is not declared; 1 is assumed.")

    # ---- the response envelope ----------------------------------------------------------
    legacy_arrays, replacement_arrays = array_paths(legacy), array_paths(replacement)
    if len(legacy_arrays) != 1 or len(replacement_arrays) != 1:
        out.problem = "the list of items cannot be identified in both responses"
        out.evidence_refs = dedupe(refs)
        return out
    response: dict[str, Any] = {"items_from": f"body:{replacement_arrays[0]}",
                                "items_to": legacy_arrays[0]}
    out.legacy_response.add(f"body:{legacy_arrays[0]}")

    successes = [(lf, rf, number) for lf, rf, number in observed
                 if number is not None and 200 <= lf.status < 300
                 and isinstance(lf.response.get("body"), dict)
                 and isinstance(rf.response.get("body"), dict)]
    fields = legacy.success_fields()
    flags = sorted(path for path, info in fields.items()
                   if info.type == "boolean" and path.count(".") == 1 and "[]" not in path)
    next_fields = sorted(
        path for path, info in replacement.success_fields().items()
        if info.leaf and path.count(".") == 1 and "[]" not in path
        and PAGINATION_ROLES.get(path[2:].lower()) == "cursor")
    if len(flags) == 1 and len(next_fields) == 1 and successes:
        flag, marker = flags[0][2:], next_fields[0][2:]
        seen = {lf.response["body"].get(flag) for lf, _, _ in successes}
        if all(lf.response["body"].get(flag) is (marker in rf.response["body"])
               for lf, rf, _ in successes):
            response["has_more_from_presence_of"] = f"body:{next_fields[0]}"
            response["has_more_to"] = flags[0]
            out.legacy_response.add(f"body:{flags[0]}")
            if seen == {True, False}:
                out.termination_established = True
                notes.append(f"{flags[0]} follows the presence of {next_fields[0]} in every "
                             "recording, for both values.")
            else:
                all_observed = False
                notes.append(f"{flags[0]} matches the presence of {next_fields[0]}, but only "
                             f"{'true' if True in seen else 'false'} was ever recorded.")
    echo: dict[str, str] = {}
    for path, info in sorted(fields.items()):
        if not info.leaf or path.count(".") != 1 or "[]" in path or info.type == "boolean":
            continue
        name = path[2:]
        values = [(lf.response["body"].get(name), number,
                   page_of(lf, limit.name) if limit else None) for lf, _, number in successes]
        values = [row for row in values if row[0] is not None]
        if not values:
            continue
        is_page = all(value == number for value, number, _ in values)
        is_limit = all(value == requested for value, _, requested in values)
        if is_page != is_limit:
            echo[path] = "$page" if is_page else "$limit"
            out.legacy_response.add(f"body:{path}")
            if len({row[0] for row in values}) < 2:
                all_observed = False
                notes.append(f"{path} equals the requested {echo[path][1:]} in the recordings, "
                             "but only one value was recorded.")
    if echo:
        response["echo"] = echo

    upstream: dict[str, Any] = {"cursor_param": cursor.name}
    if size and limit and not size_contradicted:
        upstream["size_param"] = size.name
        if not size_confirmed:
            all_observed = False
            notes.append(f"No recording confirms that {limit.name} corresponds to {size.name}.")
        out.replacement_query.add(f"query:$.{size.name}")
    legacy_block: dict[str, Any] = {"page_param": page.name,
                                    "limit_param": limit.name if limit else page.name,
                                    "first_page": first}
    if limit is None:
        out.problem = "the legacy endpoint declares a page parameter but no page-size parameter"
        out.evidence_refs = dedupe(refs)
        return out
    if limit.has_default and isinstance(limit.default, int):
        legacy_block["default_limit"] = limit.default
    if isinstance(limit.maximum, int):
        legacy_block["max_limit"] = limit.maximum
    out.transformation = {
        "strategy": "computed_cursor", "legacy": legacy_block, "upstream": upstream,
        "cursor": {"encoding": encoding, "shape": cursor_shape}, "response": response,
    }
    out.confidence = "observed" if all_observed else "inferred"
    out.rationale = " ".join(notes)
    out.evidence_refs = dedupe(refs)
    out.legacy_query = {f"query:$.{page.name}", f"query:$.{limit.name}"}
    out.replacement_query.add(f"query:$.{cursor.name}")
    return out
