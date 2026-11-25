"""The behaviour inventory (docs/architecture.md, section 6.1).

Enumerated from evidence before any check exists. It includes what the evidence
does not establish and what the product cannot analyse, so that neither can
drop out of a denominator.
"""

from __future__ import annotations

import hashlib
from typing import Any

from revenant.db.models import EvidenceFile
from revenant.reconstruction import pagination
from revenant.reconstruction.contract import EndpointContract
from revenant.reconstruction.types import BehaviourOut, GapOut, response_key


def enumerate_behaviours(legacy: list[EndpointContract], gaps: list[GapOut],
                         files: list[EvidenceFile]) -> list[BehaviourOut]:
    found: list[BehaviourOut] = []
    for endpoint in sorted(legacy, key=lambda item: (item.path, item.method)):
        name = endpoint.name
        statuses = {status for status in endpoint.statuses if status.isdigit()}
        statuses |= {str(fixture.status) for fixture in endpoint.fixtures}
        for status in sorted(statuses):
            shown = [f.ref for f in endpoint.fixtures if str(f.status) == status]
            found.append(BehaviourOut(
                key=response_key(name, status), kind="response", endpoint_id=endpoint.id,
                endpoint_ref=name,
                description=f"{name} answers {status}"
                            + ("" if shown else " (documented; no fixture shows it)"),
                evidence_refs=shown))
        if endpoint.auth:
            refused = [f.ref for f in endpoint.fixtures if f.status == 401]
            found.append(BehaviourOut(
                key=f"auth: {name}", kind="auth", endpoint_id=endpoint.id, endpoint_ref=name,
                description=f"{name} requires " + ", ".join(n for n, _, _ in endpoint.auth),
                evidence_refs=[ref for _, _, ref in endpoint.auth] + refused))
        if pagination.is_paginated(endpoint):
            page = pagination.role_param(endpoint, "page")
            pages = [f.ref for f in endpoint.fixtures
                     if page and pagination.page_of(f, page.name) is not None
                     and 200 <= f.status < 300]
            found.append(BehaviourOut(
                key=f"pagination traversal: {name}", kind="pagination_traversal",
                endpoint_id=endpoint.id, endpoint_ref=name,
                description=f"{name} returns successive pages", evidence_refs=pages))
            found.append(BehaviourOut(
                key=f"pagination termination: {name}", kind="pagination_termination",
                endpoint_id=endpoint.id, endpoint_ref=name,
                description=f"{name} signals the end of the list",
                evidence_refs=[f.ref for f in pagination.termination_fixtures(endpoint)]))
        for subject, media_types, ref in endpoint.unsupported:
            found.append(BehaviourOut(
                key=f"unsupported: {name} {subject}", kind="unsupported",
                endpoint_id=endpoint.id, endpoint_ref=name,
                description=f"{name}: {subject} body is not JSON",
                evidence_refs=[ref],
                unsupported_reason="unsupported_by_product: non-JSON body ("
                                   + ", ".join(media_types) + ")"))

    known = {item.key for item in found}
    for gap in gaps:
        if gap.behaviour_key in known:
            continue  # an existing behaviour already stands for this gap
        key = f"gap: {gap.key}"
        gap.behaviour_key = key
        found.append(BehaviourOut(
            key=key, kind="gap", endpoint_id=gap.endpoint_id, endpoint_ref=gap.endpoint_ref,
            description=gap.title, evidence_refs=[]))

    # Legacy evidence the product stored but could not read is counted, not ignored.
    for item in sorted(files, key=lambda f: (f.filename, f.sha256)):
        if item.side == "legacy" and item.parse_status in ("failed", "unsupported"):
            report: dict[str, Any] = item.parse_report or {}
            reasons = report.get("problems") or report.get("warnings") or ["not analysed"]
            found.append(BehaviourOut(
                key=f"unsupported: file {item.filename} ({item.sha256[:12]})", kind="unsupported",
                endpoint_id=None, endpoint_ref=None,
                description=f"Legacy evidence file {item.filename} was not analysed; behaviour "
                            "it describes is not in this inventory",
                evidence_refs=[{"type": "file", "evidence_file_id": item.id,
                                "filename": item.filename}],
                unsupported_reason="unsupported_by_product: " + "; ".join(
                    str(reason) for reason in reasons)))
    return found


def fingerprint(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def difference(previous: list[str] | None, current: list[str]) -> dict[str, Any]:
    """Behaviours added and removed against the previous reconstruction."""
    if previous is None:
        return {"previous_size": None, "size": len(current), "added": [], "removed": []}
    before, after = set(previous), set(current)
    return {"previous_size": len(previous), "size": len(current),
            "added": sorted(after - before), "removed": sorted(before - after)}
