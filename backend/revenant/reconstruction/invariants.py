"""Legacy-only behavioural invariants (docs/architecture.md, section 5.2).

Derived from the legacy contract and legacy fixtures alone. Nothing here reads
the replacement contract, a recording or a mapping, so an invariant cannot hold
merely because the adapter did what a mapping said.
"""

from __future__ import annotations

import re
from typing import Any

from revenant.reconstruction import correlate, pagination
from revenant.reconstruction.contract import (
    EndpointContract,
    flatten_values,
    request_values,
    response_values,
)
from revenant.reconstruction.types import InvariantOut, dedupe

_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _echo(endpoint: EndpointContract) -> list[InvariantOut]:
    """Response field Y equals request field X. ``observed`` needs two fixtures
    with different values; one fixture, or one value, gives ``inferred``."""
    good = [fixture for fixture in endpoint.fixtures if 200 <= fixture.status < 300]
    rows = [(fixture, request_values(endpoint, fixture), response_values(fixture))
            for fixture in good]
    sources = sorted({path for _, request, _ in rows for path in request})
    targets = sorted({path for _, _, response in rows for path in response if "[]" not in path})
    found: list[InvariantOut] = []
    for source in sources:
        if "[]" in source:
            continue
        for target in targets:
            values, refs, holds = [], [], True
            for fixture, request, response in rows:
                if source not in request:
                    continue
                sent = request[source][0]
                got = response.get(target, [None])[0]
                if not correlate.informative(sent) or target not in response or not (
                        sent == got and type(sent) is type(got)
                        or source.startswith(("path:", "query:")) and str(sent) == str(got)):
                    holds = False
                    break
                values.append(repr(sent))
                refs.append(fixture.ref)
            if not holds or not values:
                continue
            distinct = len(set(values))
            found.append(InvariantOut(
                key=f"echo:{endpoint.name}:{source}->{target}", endpoint_id=endpoint.id,
                endpoint_ref=endpoint.name, kind="echo",
                definition={"request": source, "response": target,
                            "statuses": sorted({f.status for f in good}),
                            "fixtures": len(values), "distinct_values": distinct},
                confidence="observed" if len(values) >= 2 and distinct >= 2 else "inferred",
                evidence_refs=dedupe(refs)))
    return found


def _schema(endpoint: EndpointContract) -> list[InvariantOut]:
    found: list[InvariantOut] = []
    for status, fields in sorted(endpoint.responses.items()):
        if not status.isdigit():
            continue
        definition: dict[str, Any] = {
            "status": int(status),
            "required": sorted(path for path, info in fields.items()
                               if info.required and path != "$"),
            "types": {path: info.type for path, info in sorted(fields.items())},
            "enums": {path: list(info.enum) for path, info in sorted(fields.items())
                      if info.enum is not None},
        }
        found.append(InvariantOut(
            key=f"schema:{endpoint.name}:{status}", endpoint_id=endpoint.id,
            endpoint_ref=endpoint.name, kind="schema", definition=definition,
            confidence="observed", evidence_refs=[endpoint.statuses[status]]))
    return found


def _pagination(endpoint: EndpointContract) -> list[InvariantOut]:
    page, limit = pagination.role_param(endpoint, "page"), pagination.role_param(
        endpoint, "limit")
    arrays = pagination.array_paths(endpoint)
    if page is None or limit is None or len(arrays) != 1:
        return []
    name = arrays[0][2:]
    pages: dict[int, list[Any]] = {}
    refs, holds = [page.ref, limit.ref], True
    for fixture in endpoint.fixtures:
        number, size = pagination.page_of(fixture, page.name), pagination.page_of(
            fixture, limit.name)
        body = fixture.response.get("body")
        if number is None or not 200 <= fixture.status < 300 or not isinstance(body, dict) \
                or not isinstance(body.get(name), list):
            continue
        items = body[name]
        if size is not None and len(items) > size:
            holds = False
        pages[number] = items
        refs.append(fixture.ref)
    seen: list[str] = []
    for number in sorted(pages):
        for item in pages[number]:
            marker = repr(item)
            if marker in seen:
                holds = False
            seen.append(marker)
    if not holds:
        return []  # the fixtures contradict it: not an invariant of this API
    return [InvariantOut(
        key=f"pagination:{endpoint.name}", endpoint_id=endpoint.id, endpoint_ref=endpoint.name,
        kind="pagination",
        definition={"page_param": page.name, "limit_param": limit.name, "items": arrays[0],
                    "checks": ["page_holds_at_most_limit_records", "no_record_on_two_pages"],
                    "pages_recorded": sorted(pages)},
        confidence="observed" if len(pages) >= 2 else "inferred",
        evidence_refs=dedupe(refs))]


def _errors(endpoint: EndpointContract) -> list[InvariantOut]:
    found: list[InvariantOut] = []
    for fixture in endpoint.fixtures:
        if fixture.status < 400:
            continue
        expect = {path: values[0] for path, values in
                  sorted(flatten_values(fixture.response.get("body")).items())
                  if len(values) == 1 and isinstance(values[0], str) and _TOKEN.match(values[0])}
        found.append(InvariantOut(
            key=f"error:{endpoint.name}:{fixture.name}", endpoint_id=endpoint.id,
            endpoint_ref=endpoint.name, kind="error",
            definition={"fixture": fixture.name, "status": fixture.status, "expect": expect,
                        "request": {"method": fixture.request["method"],
                                    "path": fixture.request["path"],
                                    "query": fixture.request.get("query") or {},
                                    "body": fixture.request.get("body")}},
            confidence="observed", evidence_refs=[fixture.ref]))
    return found


def derive(legacy: list[EndpointContract]) -> list[InvariantOut]:
    """Every invariant of the legacy side, in a deterministic order."""
    found: list[InvariantOut] = []
    for endpoint in sorted(legacy, key=lambda item: (item.path, item.method)):
        if endpoint.side != "legacy":
            raise ValueError("invariants are derived from the legacy side only")
        found += _echo(endpoint) + _schema(endpoint) + _pagination(endpoint) + _errors(endpoint)
    return found
