"""Mapping proposals and gaps for the whole project.

Every proposal names the rule that produced it and the evidence behind it. What
the evidence does not establish becomes a gap, never a mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from revenant.reconstruction import correlate, names, pagination
from revenant.reconstruction.contract import (
    EndpointContract,
    flatten_values,
    request_values,
    response_values,
)
from revenant.reconstruction.fields import (
    Decl,
    PendingEnum,
    credential_carriers,
    map_fields,
    request_decls,
    response_decls,
)
from revenant.reconstruction.pairing import Pair
from revenant.reconstruction.types import GapOut, Proposal, Ref, dedupe, response_key
from revenant.transform.model import SAFE_METHODS

# The name of a stored credential, not a credential.
DEFAULT_SECRET_REF = "replacement_credential"  # noqa: S105
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_ORDER = ("auth", "endpoint", "effect", "status_code", "pagination", "query", "header",
          "request_field", "response_field", "type", "enum", "error")


@dataclass
class Analysis:
    proposals: list[Proposal] = field(default_factory=list)
    gaps: list[GapOut] = field(default_factory=list)
    differences: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    enums: list[PendingEnum] = field(default_factory=list)


def _scoped(values: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {f"body:{path}": items for path, items in values.items()}


def _refs(decls: dict[str, Decl], paths: list[str]) -> list[Ref]:
    found = [decls[path].ref for path in paths if path in decls]
    return [ref for ref in found if ref is not None]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ---- project level ------------------------------------------------------------------------


def _auth_block(value: dict[str, Any]) -> dict[str, Any] | None:
    kind, scheme = value.get("type"), str(value.get("scheme", "")).lower()
    if kind == "apiKey" and value.get("in") == "header" and value.get("name"):
        return {"type": "api_key_header", "header": value["name"]}
    if kind == "apiKey" and value.get("in") == "query" and value.get("name"):
        return {"type": "api_key_query", "param": value["name"]}
    if kind == "http" and scheme in ("bearer", "basic"):
        return {"type": scheme}
    if kind == "basic":  # Swagger 2.0
        return {"type": "basic"}
    return None


def propose_auth(legacy: list[EndpointContract], replacement: list[EndpointContract],
                 analysis: Analysis) -> None:
    def schemes(endpoints: list[EndpointContract]) -> dict[str, tuple[dict[str, Any], Ref]]:
        found: dict[str, tuple[dict[str, Any], Ref]] = {}
        for endpoint in endpoints:
            for _name, value, ref in endpoint.auth:
                found.setdefault(repr(sorted(value.items())), (value, ref))
        return found

    inbound, outbound = schemes(legacy), schemes(replacement)
    problem = None
    if len(inbound) != 1 or len(outbound) != 1:
        problem = (f"the legacy specification declares {_plural(len(inbound), 'scheme')} and "
                   f"the replacement {_plural(len(outbound), 'scheme')}; exactly one each is "
                   "needed to propose a translation")
    else:
        (legacy_value, legacy_ref), = inbound.values()
        (replacement_value, replacement_ref), = outbound.values()
        inbound_block, outbound_block = _auth_block(legacy_value), _auth_block(replacement_value)
        if inbound_block is None or outbound_block is None:
            problem = "one of the declared schemes is of a type the product does not support"
        else:
            analysis.proposals.append(Proposal(
                key="auth", category="auth", legacy_endpoint_id=None,
                replacement_endpoint_id=None,
                legacy_ref=inbound_block["type"], replacement_ref=outbound_block["type"],
                transformation={"inbound": inbound_block,
                                "outbound": {**outbound_block, "secret_ref": DEFAULT_SECRET_REF}},
                confidence="observed", rule_name="declared_security_schemes",
                rationale="Each specification declares exactly one security scheme. Legacy "
                          "clients are verified against the project's inbound keys; the stored "
                          f"credential named {DEFAULT_SECRET_REF} is attached to upstream "
                          "requests. No credential value is part of this mapping.",
                evidence_refs=[legacy_ref, replacement_ref]))
            analysis.differences.setdefault("project", []).append({
                "category": "auth", "legacy": inbound_block, "replacement": outbound_block,
                "differences": ["authentication"] if inbound_block != outbound_block else ["none"],
                "mapping_key": "auth"})
            return
    analysis.gaps.append(GapOut(
        key="auth", category="auth", endpoint_id=None, endpoint_ref=None,
        title="Authentication translation is not established",
        description=f"No authentication mapping was proposed: {problem}.",
        missing="Specifications that declare one supported security scheme on each side.",
        severity="blocking",
        evidence_refs=dedupe([ref for _, ref in [*inbound.values(), *outbound.values()]])))


def error_envelope(legacy: list[EndpointContract], analysis: Analysis) -> dict[str, str]:
    """The legacy error shape as ``{variable name: path}``; empty if not established."""
    shapes: dict[tuple[str, ...], list[Ref]] = {}
    for endpoint in legacy:
        for fixture in endpoint.fixtures:
            if fixture.status >= 400 and isinstance(fixture.response.get("body"), dict):
                paths = tuple(sorted(flatten_values(fixture.response["body"])))
                shapes.setdefault(paths, []).append(fixture.ref)
    basis = "every recorded legacy error response has this shape"
    if not shapes:
        for endpoint in legacy:
            for status, fields in sorted(endpoint.responses.items()):
                if status.isdigit() and int(status) >= 400:
                    paths = tuple(sorted(path for path, info in fields.items() if info.leaf))
                    shapes.setdefault(paths, []).append(endpoint.statuses[status])
        basis = "every documented legacy error response has this shape"
    if not shapes:
        return {}
    if len(shapes) > 1 or any("[]" in path for path in next(iter(shapes))) or not next(
            iter(shapes)):
        analysis.gaps.append(GapOut(
            key="error_envelope", category="error", endpoint_id=None, endpoint_ref=None,
            title="The legacy error format is not uniform",
            description=f"Legacy error responses have {len(shapes)} different shapes, so no "
                        "single error envelope can be proposed.",
            missing="Evidence of one error format, or a manual mapping per endpoint.",
            severity="advisory",
            evidence_refs=dedupe([ref for refs in shapes.values() for ref in refs])))
        return {}
    (paths, refs), = shapes.items()
    leaves = [names.leaf(path) for path in paths]
    variables = {(names.leaf(path) if leaves.count(names.leaf(path)) == 1
                  else "_".join(names.segments(path))): path for path in paths}
    analysis.proposals.append(Proposal(
        key="error_envelope", category="error", legacy_endpoint_id=None,
        replacement_endpoint_id=None, legacy_ref="error body: " + ", ".join(paths),
        replacement_ref="variables set by error rules",
        transformation={"kind": "error_envelope", "rules": [
            {"op": "move", "from": f"var:$.{name}", "to": path}
            for name, path in sorted(variables.items())]},
        confidence="observed", rule_name="legacy_error_shape",
        rationale=f"The legacy error body is built from these fields: {basis}.",
        evidence_refs=dedupe(refs)))
    return variables


# ---- one pair -----------------------------------------------------------------------------


def _effect(pair: Pair) -> Proposal:
    legacy, replacement = pair.legacy, pair.replacement
    markers = [(endpoint.name, *endpoint.effect_marker) for endpoint in (legacy, replacement)
               if endpoint.effect_marker]
    refs = [ref for _, _, ref in markers]
    unsafe = [endpoint.name for endpoint in (legacy, replacement)
              if endpoint.method not in SAFE_METHODS]
    marked = [name for name, effect, _ in markers if effect == "state_changing"]
    if unsafe:
        effect, confidence, rule = "state_changing", "observed", "unsafe_method"
        rationale = f"{' and '.join(unsafe)} use a method that is not GET, HEAD or OPTIONS."
    elif marked:
        effect, confidence, rule = "state_changing", "observed", "evidence_marker"
        rationale = (f"The specification marks {' and '.join(marked)} as state-changing "
                     "(x-revenant-effect), although the method is safe.")
    else:
        effect, confidence, rule = "unknown", "inferred", "safe_methods_prove_nothing"
        rationale = ("Both methods are safe, which is not evidence that the operation changes "
                     "nothing. It is treated as state-changing until a person declares it "
                     "read-only.")
        if any(value == "read_only" for _, value, _ in markers):
            rationale += (" A specification marks it read-only; that is recorded as evidence "
                          "and does not replace the declaration.")
    return Proposal(
        key=f"effect:{legacy.name}", category="effect", legacy_endpoint_id=legacy.id,
        replacement_endpoint_id=replacement.id, legacy_ref=legacy.name,
        replacement_ref=replacement.name, transformation={"effect": effect},
        confidence=confidence, rule_name=rule, rationale=rationale, evidence_refs=refs)


def _statuses(pair: Pair, analysis: Analysis) -> None:
    legacy, replacement = pair.legacy, pair.replacement
    seen: dict[int, dict[int, list[Ref]]] = {}
    for lf, rf in pair.fixture_pairs:
        if 200 <= lf.status < 300:
            seen.setdefault(rf.status, {}).setdefault(lf.status, []).extend([lf.ref, rf.ref])

    def add(source: int, target: int, confidence: str, rule: str, rationale: str,
            refs: list[Ref]) -> None:
        key = f"status_code:{legacy.name}:{source}"
        analysis.proposals.append(Proposal(
            key=key, category="status_code", legacy_endpoint_id=legacy.id,
            replacement_endpoint_id=replacement.id, legacy_ref=str(target),
            replacement_ref=str(source),
            transformation={"section": "response.status", "from": source, "to": target},
            confidence=confidence, rule_name=rule, rationale=rationale,
            evidence_refs=dedupe(refs)))
        analysis.differences.setdefault(legacy.id, []).append({
            "category": "status_code", "legacy": target, "replacement": source,
            "differences": ["status_code"] if source != target else ["none"],
            "mapping_key": key})

    for source in sorted(seen):
        if len(seen[source]) == 1:
            (target, refs), = seen[source].items()
            count = len(refs) // 2
            add(source, target, "observed", "status_fixture_pair",
                f"The replacement answered {source} where the legacy API answered {target} in "
                f"{_plural(count, 'paired recording')}.", refs)
        else:
            analysis.gaps.append(GapOut(
                key=f"status_code:{legacy.name}:{source}:conflict", category="status_code",
                endpoint_id=legacy.id, endpoint_ref=legacy.name,
                title=f"{legacy.name}: replacement status {source} maps to several legacy "
                      "statuses",
                description="Paired recordings show the same replacement success status for "
                            f"legacy statuses {sorted(seen[source])}.",
                missing="A rule that tells these cases apart.", severity="advisory",
                evidence_refs=dedupe([r for refs in seen[source].values() for r in refs])))
    if seen:
        return

    def successes(endpoint: EndpointContract) -> list[str]:
        return sorted(code for code in endpoint.statuses
                      if code.isdigit() and 200 <= int(code) < 300)

    ours, theirs = successes(legacy), successes(replacement)
    if len(ours) == 1 and len(theirs) == 1:
        refs = [legacy.statuses[ours[0]], replacement.statuses[theirs[0]]]
        if ours == theirs:
            add(int(theirs[0]), int(ours[0]), "observed", "identical_status_in_both_specifications",
                f"Both specifications document {ours[0]} as the only success status.", refs)
        else:
            add(int(theirs[0]), int(ours[0]), "inferred", "single_success_status",
                f"Each specification documents one success status ({theirs[0]} and {ours[0]}). "
                "No recording confirms that they correspond.", refs)


def _errors(pair: Pair, variables: dict[str, str], analysis: Analysis) -> None:
    legacy, replacement = pair.legacy, pair.replacement
    paired_ids = {lf.id for lf, _ in pair.fixture_pairs}
    unpaired = [fixture for fixture in legacy.fixtures
                if fixture.status >= 400 and fixture.id not in paired_ids]
    if unpaired:
        analysis.gaps.append(GapOut(
            key=f"error:{legacy.name}:unpaired", category="error", endpoint_id=legacy.id,
            endpoint_ref=legacy.name,
            title=f"{legacy.name}: legacy errors with no replacement recording",
            description="These recorded legacy errors have no paired replacement recording, so "
                        "the replacement response that should produce them is not known: "
                        + ", ".join(f"{f.name} ({f.status})" for f in unpaired) + ".",
            missing="A paired replacement recording for each.", severity="advisory",
            evidence_refs=[fixture.ref for fixture in unpaired]))
    rules: dict[str, tuple[dict[str, Any], str, str, list[Ref]]] = {}
    for lf, rf in pair.fixture_pairs:
        if lf.status < 400 or not variables:
            continue
        ours, theirs = flatten_values(lf.response.get("body")), flatten_values(
            rf.response.get("body"))
        assigned: dict[str, dict[str, Any]] = {}
        when: dict[str, Any] = {"status_in": [rf.status]}
        discriminator = ""
        for name, path in sorted(variables.items()):
            if path not in ours or len(ours[path]) != 1:
                continue
            value = ours[path][0]
            same = sorted(q for q, items in theirs.items() if len(items) == 1
                          and "[]" not in q and correlate.informative(value)
                          and type(items[0]) is type(value) and items[0] == value)
            if len(same) > 1:
                named = [q for q in same if names.same_name(names.leaf(q), names.leaf(path))]
                same = named if len(named) == 1 else []
            if len(same) == 1:
                assigned[name] = {"from": f"body:{same[0]}"}
                continue
            assigned[name] = {"const": value}
            for q, items in sorted(theirs.items()):
                if (not discriminator and len(items) == 1 and isinstance(items[0], str)
                        and _TOKEN.match(items[0]) and "[]" not in q
                        and names.same_name(names.leaf(q), names.leaf(path))):
                    when["equals"] = [f"body:{q}", items[0]]
                    discriminator = items[0]
        if not assigned:
            continue
        key = f"error:{legacy.name}:{rf.status}:{discriminator or '*'}"
        transformation = {"kind": "error_rule", "when": when, "status": lf.status,
                          "vars": assigned}
        if key in rules and rules[key][0] != transformation:
            analysis.gaps.append(GapOut(
                key=f"{key}:conflict", category="error", endpoint_id=legacy.id,
                endpoint_ref=legacy.name,
                title=f"{legacy.name}: replacement error {rf.status} is ambiguous",
                description="Two recordings show the same replacement error producing "
                            "different legacy errors, and nothing in the replacement response "
                            "tells them apart.",
                missing="A field in the replacement error that distinguishes the cases.",
                severity="advisory", evidence_refs=dedupe([*rules[key][3], lf.ref, rf.ref])))
            continue
        previous = rules[key][3] if key in rules else []
        rules[key] = (transformation, f"{lf.status}", f"{rf.status} {discriminator}".strip(),
                      [*previous, lf.ref, rf.ref])
    for key in sorted(rules):
        transformation, legacy_ref, replacement_ref, refs = rules[key]
        analysis.proposals.append(Proposal(
            key=key, category="error", legacy_endpoint_id=legacy.id,
            replacement_endpoint_id=replacement.id, legacy_ref=legacy_ref,
            replacement_ref=replacement_ref, transformation=transformation,
            confidence="observed", rule_name="error_fixture_pair",
            rationale=f"A paired recording shows the replacement answering {replacement_ref} "
                      f"where the legacy API answered {legacy_ref}. Values are copied where the "
                      "two bodies agree and fixed where they differ.",
            evidence_refs=dedupe(refs)))
        analysis.differences.setdefault(legacy.id, []).append({
            "category": "error", "legacy": legacy_ref, "replacement": replacement_ref,
            "differences": ["error_format"], "mapping_key": key})


def _unmapped_gap(legacy: EndpointContract, key: str, category: str, title: str,
                  paths: list[str], notes: dict[str, str], required: set[str],
                  consequence: str, missing: str, refs: list[Ref], blocking: bool) -> GapOut:
    lines = [f"{path}{' (required)' if path in required else ''}"
             + (f": {notes[path]}" if path in notes else "") for path in paths]
    return GapOut(
        key=key, category=category, endpoint_id=legacy.id, endpoint_ref=legacy.name,
        title=f"{legacy.name}: {title}", description=f"{consequence} " + "; ".join(lines) + ".",
        missing=missing, severity="blocking" if blocking else "advisory",
        evidence_refs=dedupe(refs))


def analyse_pair(pair: Pair, variables: dict[str, str], carriers: set[str],
                 analysis: Analysis) -> None:
    legacy, replacement = pair.legacy, pair.replacement
    differences = analysis.differences.setdefault(legacy.id, [])
    pair_refs = [ref for lf, rf in pair.fixture_pairs for ref in (lf.ref, rf.ref)]

    # ---- pagination first: it owns its parameters and envelope fields -----------------
    paging = pagination.detect(pair)
    if paging is not None and paging.transformation is not None:
        key = f"pagination:{legacy.name}"
        analysis.proposals.append(Proposal(
            key=key, category="pagination", legacy_endpoint_id=legacy.id,
            replacement_endpoint_id=replacement.id,
            legacy_ref="page-numbered: " + ", ".join(sorted(paging.legacy_query)),
            replacement_ref="cursor: " + ", ".join(sorted(paging.replacement_query)),
            transformation=paging.transformation, confidence=paging.confidence,
            rule_name="computed_cursor_from_recordings", rationale=paging.rationale,
            evidence_refs=paging.evidence_refs))
        differences.append({"category": "pagination", "legacy": "page number",
                            "replacement": "cursor", "differences": ["pagination"],
                            "mapping_key": key})
    elif paging is not None:
        analysis.gaps.append(GapOut(
            key=f"pagination:{legacy.name}", category="pagination", endpoint_id=legacy.id,
            endpoint_ref=legacy.name, title=f"{legacy.name}: pagination is not established",
            description=f"No pagination mapping was proposed: {paging.problem}.",
            missing="Paired recordings of at least two pages.", severity="blocking",
            evidence_refs=paging.evidence_refs))
    if pagination.is_paginated(legacy) and not (paging and paging.termination_established) \
            and not pagination.termination_fixtures(legacy):
        analysis.gaps.append(GapOut(
            key=f"pagination:{legacy.name}:termination", category="pagination",
            endpoint_id=legacy.id, endpoint_ref=legacy.name,
            title=f"{legacy.name}: how the list ends is not established",
            description="No recording shows the last page or a page beyond it, so what the "
                        "legacy API returned at the end of the list is unknown.",
            missing="A recorded legacy response for the last page, with its paired recording.",
            severity="advisory", behaviour_key=f"pagination termination: {legacy.name}"))
    mapped = paging is not None and paging.transformation is not None

    # ---- request ------------------------------------------------------------------------
    request = map_fields(
        direction="request", legacy=legacy, replacement=replacement,
        observations=[(request_values(legacy, lf), request_values(replacement, rf))
                      for lf, rf in pair.fixture_pairs],
        observation_refs=[[lf.ref, rf.ref] for lf, rf in pair.fixture_pairs],
        source_decls=request_decls(legacy, carriers),
        target_decls=request_decls(replacement, carriers),
        excluded_sources=(paging.legacy_query if paging and mapped else set()) | carriers,
        excluded_targets=(paging.replacement_query if paging and mapped else set()) | carriers,
    )
    # ---- response (success bodies; errors have their own rules) -------------------------
    good = [(lf, rf) for lf, rf in pair.fixture_pairs if 200 <= lf.status < 300]
    response = map_fields(
        direction="response", legacy=legacy, replacement=replacement,
        observations=[(_scoped(response_values(rf)), _scoped(response_values(lf)))
                      for lf, rf in good],
        observation_refs=[[lf.ref, rf.ref] for lf, rf in good],
        source_decls=response_decls(replacement), target_decls=response_decls(legacy),
        excluded_sources=set(),
        excluded_targets=paging.legacy_response if paging and mapped else set(),
    )
    for outcome in (request, response):
        analysis.proposals.extend(outcome.proposals)
        analysis.enums.extend(outcome.enums)
        differences.extend(outcome.differences)

    # ---- the route itself ---------------------------------------------------------------
    wanted = [name for name in replacement.path_params()]
    params: list[dict[str, str]] = []
    route_confidence, route_refs, notes = pair.confidence, list(pair_refs), []
    unresolved = [name for name in wanted if name not in request.path_params]
    if unresolved and len(wanted) == len(legacy.path_params()):
        # No recording shows the values: fall back to position, and say so.
        for ours, theirs in zip(legacy.path_params(), wanted, strict=True):
            if theirs in unresolved:
                request.path_params[theirs] = (f"path:$.{ours}", "inferred", [])
                request.used_sources.add(f"path:$.{ours}")
                notes.append(f"{{{theirs}}} is taken from {{{ours}}} by position; no recording "
                             "confirms it.")
        unresolved = []
    if unresolved:
        analysis.gaps.append(GapOut(
            key=f"endpoint:{legacy.name}:path_parameters", category="endpoint",
            endpoint_id=legacy.id, endpoint_ref=legacy.name,
            title=f"{legacy.name}: replacement path parameters cannot be filled",
            description=f"Nothing establishes the value of {', '.join(unresolved)} in "
                        f"{replacement.name}.",
            missing="A paired recording that shows the replacement path.", severity="blocking",
            evidence_refs=route_refs))
    else:
        for name in wanted:
            source, confidence, refs = request.path_params[name]
            params.append({"from": source, "to": name})
            route_refs += refs
            if confidence == "inferred":
                route_confidence = "inferred"
        how = (f"{_plural(len(pair.fixture_pairs), 'recording')} of {replacement.name} "
               f"{'is' if len(pair.fixture_pairs) == 1 else 'are'} paired with fixtures of "
               f"{legacy.name}." if pair.confidence == "observed" else
               f"{replacement.name} is the most similar replacement endpoint (score "
               f"{pair.score}) by method, path shape and field names. No recording confirms "
               "the correspondence.")
        key = f"endpoint:{legacy.name}"
        analysis.proposals.append(Proposal(
            key=key, category="endpoint", legacy_endpoint_id=legacy.id,
            replacement_endpoint_id=replacement.id, legacy_ref=legacy.name,
            replacement_ref=replacement.name,
            transformation={"match": {"method": legacy.method, "path": legacy.path},
                            "upstream": {"method": replacement.method,
                                         "path": replacement.path},
                            "path_params": params},
            confidence=route_confidence,
            rule_name="paired_recordings" if pair.confidence == "observed"
            else "endpoint_similarity",
            rationale=" ".join([how, *notes]), evidence_refs=dedupe(route_refs)))
        changed = [name for name, a, b in (("method", legacy.method, replacement.method),
                                           ("path", legacy.path, replacement.path)) if a != b]
        differences.append({"category": "endpoint", "legacy": legacy.name,
                            "replacement": replacement.name,
                            "differences": changed or ["none"], "mapping_key": key})
    analysis.proposals.append(_effect(pair))
    _statuses(pair, analysis)
    _errors(pair, variables, analysis)

    # ---- what was not established --------------------------------------------------------
    target_decls = request_decls(replacement, carriers)
    excluded = paging.replacement_query if paging and mapped else set()
    needed = sorted(path for path, decl in target_decls.items()
                    if decl.required and path not in request.mapped_targets
                    and path not in excluded and not path.startswith("path:"))
    if needed:
        analysis.gaps.append(_unmapped_gap(
            legacy, f"request_field:{legacy.name}:required", "request_field",
            "required replacement request fields have no source", needed, request.notes,
            set(needed), f"{replacement.name} requires fields that nothing in the legacy "
            "request is known to supply:", "Paired recordings that show these fields, or a "
            "manual mapping.", _refs(target_decls, needed),
            blocking=True))
    source_decls = request_decls(legacy, carriers)
    consumed = paging.legacy_query if paging and mapped else set()
    seen = {path for lf, _ in pair.fixture_pairs for path in request_values(legacy, lf)}
    dropped = sorted(path for path in (set(source_decls) | seen) - carriers
                     if path not in request.used_sources and path not in consumed)
    if dropped:
        analysis.gaps.append(_unmapped_gap(
            legacy, f"request_field:{legacy.name}:unmapped", "request_field",
            "legacy request fields have no established destination", dropped, {},
            {p for p in dropped if p in source_decls and source_decls[p].required},
            "These legacy request fields would not reach the replacement:",
            "Paired recordings that show where these values go, or a manual mapping.",
            _refs(source_decls, dropped),
            blocking=False))
    legacy_response = response_decls(legacy)
    produced = paging.legacy_response if paging and mapped else set()
    shown = {path for lf, _ in good for path in _scoped(response_values(lf))}
    absent = sorted(path for path in set(legacy_response) | shown
                    if path not in response.mapped_targets and path not in produced)
    if absent:
        required = {p for p in absent if p in legacy_response and legacy_response[p].required}
        analysis.gaps.append(_unmapped_gap(
            legacy, f"response_field:{legacy.name}:unmapped", "response_field",
            "legacy response fields cannot be produced", absent, response.notes, required,
            "Nothing establishes where these legacy response fields come from:",
            "Paired recordings of this operation, or a manual mapping.",
            _refs(legacy_response, absent),
            blocking=bool(required)))


def evidence_gaps(legacy: list[EndpointContract], analysis: Analysis) -> None:
    """Documented status codes that no fixture shows. Each stands for the response
    behaviour it concerns, so it is not counted twice in the inventory."""
    for endpoint in legacy:
        shown = {str(fixture.status) for fixture in endpoint.fixtures}
        for status in sorted(endpoint.statuses):
            if status.isdigit() and status not in shown:
                analysis.gaps.append(GapOut(
                    key=f"status_code:{endpoint.name}:{status}:no_example",
                    category="status_code", endpoint_id=endpoint.id, endpoint_ref=endpoint.name,
                    title=f"{endpoint.name}: status {status} is documented but never recorded",
                    description=f"The specification documents status {status}, but no fixture "
                                "shows what the legacy API actually returned.",
                    missing=f"A recorded {status} response.", severity="advisory",
                    evidence_refs=[endpoint.statuses[status]],
                    behaviour_key=response_key(endpoint.name, status)))


def finish_enums(analysis: Analysis) -> None:
    """Complete enumeration mappings from the same enumeration seen on other endpoints."""
    registry: dict[tuple[frozenset[Any], frozenset[Any]], dict[str, Any] | None] = {}
    for item in analysis.enums:
        slot = (frozenset(item.source_enum), frozenset(item.target_enum))
        known = registry.get(slot, {})
        if known is None:
            continue
        merged = dict(known)
        for source, target in item.observed.items():
            if merged.setdefault(source, target) != target:
                registry[slot] = None  # endpoints disagree: nothing is borrowed
                break
        else:
            registry[slot] = merged
    for item in analysis.enums:
        proposal, mapping = item.proposal, dict(item.observed)
        borrowed = registry.get((frozenset(item.source_enum), frozenset(item.target_enum))) or {}
        notes: list[str] = []
        for source, target in sorted(borrowed.items()):
            if source not in mapping and target not in mapping.values():
                mapping[source] = target
                notes.append(f"{source} → {target} was observed on another endpoint with the "
                             "same enumerations")
        left = [value for value in item.source_enum if value not in mapping]
        free = [value for value in item.target_enum if value not in mapping.values()]
        if len(left) == 1 and len(free) == 1:
            mapping[left[0]] = free[0]
            notes.append(f"{left[0]} → {free[0]} is the only pairing left")
            left = []
        if notes:
            proposal.confidence = "inferred"
            proposal.rationale += " Not every value was recorded here: " + "; ".join(notes) + "."
        proposal.transformation["rule"]["map"] = {key: mapping[key] for key in sorted(mapping)}
        analysis.proposals.append(proposal)
        if left:
            analysis.gaps.append(GapOut(
                key=f"enum:{proposal.key}", category="enum",
                endpoint_id=proposal.legacy_endpoint_id,
                endpoint_ref=item.endpoint_ref,
                title=f"{proposal.legacy_ref}: enumeration values with no counterpart",
                description="No recording shows what these values correspond to: "
                            + ", ".join(str(value) for value in left)
                            + ". The adapter fails explicitly if one of them occurs.",
                missing="A paired recording for each value.", severity="advisory",
                evidence_refs=proposal.evidence_refs))


def analyse(legacy: list[EndpointContract], replacement: list[EndpointContract],
            pairs: list[Pair], unpaired: list[tuple[EndpointContract, str]]) -> Analysis:
    analysis = Analysis()
    propose_auth(legacy, replacement, analysis)
    variables = error_envelope(legacy, analysis)
    carriers = credential_carriers(
        [value for endpoint in [*legacy, *replacement] for _, value, _ in endpoint.auth])
    for pair in pairs:
        analyse_pair(pair, variables, carriers, analysis)
    finish_enums(analysis)
    for endpoint, reason in unpaired:
        analysis.gaps.append(GapOut(
            key=f"endpoint:{endpoint.name}:unpaired", category="endpoint",
            endpoint_id=endpoint.id, endpoint_ref=endpoint.name,
            title=f"{endpoint.name}: no replacement endpoint",
            description=f"No replacement endpoint was paired with it: {reason}.",
            missing="A paired replacement recording, or a manual endpoint mapping.",
            severity="blocking", evidence_refs=[f.ref for f in endpoint.fixtures]))
    evidence_gaps(legacy, analysis)

    order = {pair.legacy.id: index for index, pair in enumerate(pairs)}
    analysis.proposals.sort(key=lambda p: (
        order.get(p.legacy_endpoint_id or "", -1), _ORDER.index(p.category), p.key))
    analysis.gaps.sort(key=lambda g: (g.endpoint_ref or "", g.key))
    return analysis

