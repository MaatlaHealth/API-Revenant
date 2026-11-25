"""Field mapping proposers for one endpoint pair and one direction.

Order of preference for each target field:

1. fixture value correlation — the same values appear at the source path in
   paired recordings (``observed``);
2. enumeration correlation — two enumerated fields move together one-to-one;
3. names only, when no fixture shows the target at all: identical name and type
   (``observed``), then normalised name, then structural match (``inferred``).

A target that none of these establishes gets no mapping. The caller records it
as a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from revenant.reconstruction import correlate, names
from revenant.reconstruction.contract import EndpointContract, FieldInfo
from revenant.reconstruction.types import Proposal, Ref, dedupe
from revenant.transform.model import CREDENTIAL_HEADERS, check_pointer, is_name

SECTIONS = {"body": "body", "query": "query", "header": "headers"}


@dataclass(frozen=True)
class Decl:
    """What a specification declares about one field or parameter."""

    type: str
    required: bool
    enum: tuple[Any, ...] | None = None
    has_default: bool = False
    default: Any = None
    ref: Ref | None = None

    @classmethod
    def of(cls, info: FieldInfo) -> Decl:
        return cls(info.type, info.required, info.enum, info.has_default, info.default, info.ref)


@dataclass
class PendingEnum:
    """An enumeration mapping that may still be completed from other endpoints."""

    proposal: Proposal
    endpoint_ref: str
    source_enum: tuple[Any, ...]
    target_enum: tuple[Any, ...]
    observed: dict[str, Any]


@dataclass
class Outcome:
    proposals: list[Proposal] = field(default_factory=list)
    enums: list[PendingEnum] = field(default_factory=list)
    differences: list[dict[str, Any]] = field(default_factory=list)
    used_sources: set[str] = field(default_factory=set)
    mapped_targets: set[str] = field(default_factory=set)
    # replacement path parameter -> (source path, confidence, evidence)
    path_params: dict[str, tuple[str, str, list[Ref]]] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)  # target -> why it has no mapping


def request_decls(endpoint: EndpointContract, carriers: set[str]) -> dict[str, Decl]:
    out = {f"body:{path}": Decl.of(info) for path, info in endpoint.request_fields.items()
           if info.leaf}
    for param in endpoint.params:
        if param.location in ("query", "path", "header") and is_name(param.name):
            out[f"{param.location}:$.{param.name}"] = Decl(
                param.type, param.required, None, param.has_default, param.default, param.ref)
    for name in endpoint.path_params():
        if is_name(name):
            out.setdefault(f"path:$.{name}", Decl("string", True))
    return {path: decl for path, decl in out.items() if path.lower() not in carriers}


def response_decls(endpoint: EndpointContract) -> dict[str, Decl]:
    return {f"body:{path}": Decl.of(info) for path, info in endpoint.success_fields().items()
            if info.leaf}


def credential_carriers(schemes: list[dict[str, Any]]) -> set[str]:
    """Scoped paths (lower case) that carry credentials and are outside every mapping."""
    found = {f"header:$.{name}" for name in CREDENTIAL_HEADERS}
    for scheme in schemes:
        if scheme.get("type") == "apiKey" and scheme.get("in") in ("header", "query"):
            found.add(f"{scheme['in']}:$.{str(scheme.get('name', '')).lower()}")
    return found


def _compatible(left: str, right: str) -> bool:
    numeric = {"integer", "number"}
    return left == right or "any" in (left, right) or {left, right} <= numeric


def build_rule(source: str, target: str) -> tuple[str, str, dict[str, str] | None] | None:
    """Rule ``from`` and ``to`` for a source and target, with the ``each`` wrapper if
    both are inside one array. None if the language cannot express the mapping."""
    source_scope, _, source_pointer = source.partition(":")
    _target_scope, _, target_pointer = target.partition(":")
    depth = source_pointer.count("[]")
    if depth != target_pointer.count("[]") or depth > 1:
        return None
    try:
        if depth == 0:
            check_pointer(source_pointer)
            check_pointer(target_pointer)
            return source, target_pointer, None
        source_array, source_rest = source_pointer.split("[]")
        target_array, target_rest = target_pointer.split("[]")
        for pointer in (source_array, "$" + source_rest, target_array, "$" + target_rest):
            check_pointer(pointer)
    except ValueError:
        return None
    return ("item:$" + source_rest, "$" + target_rest,
            {"from": f"{source_scope}:{source_array}", "to": target_array})


def _difference(category: str, direction: str, legacy: str, replacement: str,
                legacy_decl: Decl | None, replacement_decl: Decl | None,
                key: str) -> dict[str, Any]:
    kinds: list[str] = []
    legacy_pointer, replacement_pointer = legacy.split(":", 1)[1], replacement.split(":", 1)[1]
    if legacy.split(":", 1)[0] != replacement.split(":", 1)[0]:
        kinds.append("location")
    if names.leaf(legacy_pointer) != names.leaf(replacement_pointer):
        kinds.append("renamed")
    if names.segments(legacy_pointer)[:-1] != names.segments(replacement_pointer)[:-1]:
        kinds.append("nesting")
    if legacy_decl and replacement_decl:
        if legacy_decl.type != replacement_decl.type:
            kinds.append("type")
        if legacy_decl.required != replacement_decl.required:
            kinds.append("required")
        if (legacy_decl.has_default, legacy_decl.default) != (
                replacement_decl.has_default, replacement_decl.default):
            kinds.append("default")
        if legacy_decl.enum != replacement_decl.enum:
            kinds.append("enum_values")
    return {"category": category, "direction": direction, "legacy": legacy,
            "replacement": replacement, "differences": kinds or ["none"], "mapping_key": key}


def map_fields(
    *, direction: str, legacy: EndpointContract, replacement: EndpointContract,
    observations: list[correlate.Observation], observation_refs: list[list[Ref]],
    source_decls: dict[str, Decl], target_decls: dict[str, Decl],
    excluded_sources: set[str], excluded_targets: set[str],
) -> Outcome:
    """Propose a mapping for every target field of one direction of one pair."""
    out = Outcome()
    seen_sources = {path for source, _ in observations for path in source}
    seen_targets = {path for _, target in observations for path in target}
    sources = sorted((set(source_decls) | seen_sources) - excluded_sources)
    targets = sorted((set(target_decls) | seen_targets) - excluded_targets)

    def refs_for(indexes: tuple[int, ...], *decls: Decl | None) -> list[Ref]:
        found = [decl.ref for decl in decls if decl and decl.ref]
        for index in indexes:
            found.extend(observation_refs[index])
        return dedupe(found)

    for target in targets:
        target_decl = target_decls.get(target)
        target_scope, _, target_pointer = target.partition(":")
        found = [item for item in correlate.candidates(observations, target)
                 if item.source in sources]
        if len(found) > 1:
            named = [item for item in found
                     if names.same_name(names.leaf(item.source), names.leaf(target))]
            if len(named) == 1:
                found = named
        chosen: tuple[str, str, str, str, str, tuple[int, ...]] | None = None
        mapping: dict[str, Any] | None = None
        if len(found) == 1:
            item = found[0]
            count = len(item.observations)
            rationale = (
                f"The value at {item.source} matches the value at {target} in {count} paired "
                f"recording{'s' if count != 1 else ''} ({item.distinct} distinct "
                f"value{'s' if item.distinct != 1 else ''})"
                + ("" if item.conversion == "identity" else f" after {item.conversion}") + ".")
            confidence = "observed"
            if item.weak:
                confidence = "inferred"
                rationale += " The only value seen is a small number, which could be a coincidence."
            chosen = (item.source, item.conversion, confidence, "fixture_value_correlation",
                      rationale, item.observations)
        elif len(found) > 1:
            out.notes[target] = ("several fields carry the same values in the recordings: "
                                 + ", ".join(item.source for item in found))
        else:
            enum_found = []
            if target_decl and target_decl.enum:
                for source in sources:
                    decl = source_decls.get(source)
                    if decl and decl.enum and len(decl.enum) == len(target_decl.enum):
                        result = correlate.enum_map(observations, source, target, decl.enum,
                                                    target_decl.enum)
                        if result:
                            enum_found.append((source, *result))
                if len(enum_found) > 1:
                    named_enums = [row for row in enum_found
                                   if names.same_name(names.leaf(row[0]), names.leaf(target))]
                    enum_found = named_enums if len(named_enums) == 1 else enum_found
            if len(enum_found) == 1:
                source, mapping, indexes = enum_found[0]
                size = len(target_decl.enum or ()) if target_decl else 0
                rationale = (f"{source} and {target} are both enumerations of {size} values "
                             f"and move together in {len(indexes)} paired "
                             f"recording{'s' if len(indexes) != 1 else ''}.")
                chosen = (source, "enum", "observed", "enum_value_correlation", rationale, indexes)
            elif len(enum_found) > 1:
                out.notes[target] = "several enumerated fields could correspond to it"
            elif target_decl is not None and target not in seen_targets:
                chosen = _by_name(target, target_decl, sources, source_decls, seen_sources, out)
            elif target in seen_targets:
                out.notes[target] = "no field in the paired recordings carries its value"

        if chosen is None:
            continue
        source, conversion, confidence, rule_name, rationale, indexes = chosen
        source_decl = source_decls.get(source)
        refs = refs_for(indexes, source_decl, target_decl)

        if target_scope == "path":
            if conversion == "identity":
                out.path_params[target_pointer[2:]] = (source, confidence, refs)
                out.used_sources.add(source)
                out.mapped_targets.add(target)
            continue
        built = build_rule(source, target)
        if built is None or target_scope not in SECTIONS:
            out.notes[target] = (f"{source} appears to correspond, but the transformation "
                                 "language cannot express a mapping across these array levels")
            continue
        rule_from, rule_to, each = built
        section = f"{direction}.{SECTIONS[target_scope]}"
        rule: dict[str, Any] = {"op": "move", "from": rule_from, "to": rule_to}
        if conversion == "enum":
            rule = {"op": "enum_map", "from": rule_from, "to": rule_to, "map": mapping or {},
                    "on_unmapped": "error"}
            category = "enum"
        elif conversion != "identity":
            rule = {"op": "convert", "from": rule_from, "to": rule_to, "as": conversion}
            category = "type"
        elif direction == "response":
            category = "response_field"
        else:
            category = {"query": "query", "header": "header"}.get(target_scope, "request_field")
        if target_decl is not None and target_decl.required:
            rule["required"] = True
        transformation: dict[str, Any] = {"section": section, "rule": rule}
        if each:
            transformation["each"] = each
        legacy_path, replacement_path = (source, target) if direction == "request" else (
            target, source)
        legacy_decl, replacement_decl = (source_decl, target_decl) if direction == "request" \
            else (target_decl, source_decl)
        key = f"field:{legacy.name}:{section}:{target_pointer}"
        proposal = Proposal(
            key=key, category=category, legacy_endpoint_id=legacy.id,
            replacement_endpoint_id=replacement.id,
            legacy_ref=f"{direction}.{legacy_path}",
            replacement_ref=f"{direction}.{replacement_path}",
            transformation=transformation, confidence=confidence, rule_name=rule_name,
            rationale=rationale, evidence_refs=refs,
        )
        if conversion == "enum" and source_decl and target_decl:
            out.enums.append(PendingEnum(proposal, legacy.name, source_decl.enum or (),
                                         target_decl.enum or (), dict(mapping or {})))
        else:
            out.proposals.append(proposal)
        out.differences.append(_difference(category, direction, legacy_path, replacement_path,
                                           legacy_decl, replacement_decl, key))
        out.used_sources.add(source)
        out.mapped_targets.add(target)
    return out


def _by_name(target: str, target_decl: Decl, sources: list[str],
             source_decls: dict[str, Decl], seen_sources: set[str],
             out: Outcome) -> tuple[str, str, str, str, str, tuple[int, ...]] | None:
    """Name-based proposals, used only when no fixture shows the target field."""
    target_scope, _, target_pointer = target.partition(":")
    declared = [(source, source_decls[source]) for source in sources
                if source in source_decls and source not in seen_sources]
    mismatched: list[str] = []

    def usable(source: str, decl: Decl) -> bool:
        if _compatible(decl.type, target_decl.type):
            return True
        mismatched.append(f"{source} ({decl.type} against {target_decl.type})")
        return False

    for source, decl in declared:
        if source == target and usable(source, decl):
            return (source, "identity", "observed", "identical_name_and_type",
                    f"{target} has the same name, position and type in both specifications.", ())
    same_place = [(source, decl) for source, decl in declared
                  if source.partition(":")[0] == target_scope
                  and names.same_path(source.partition(":")[2], target_pointer)]
    usable_same = [(source, decl) for source, decl in same_place if usable(source, decl)]
    if len(usable_same) == 1:
        source = usable_same[0][0]
        return (source, "identity", "inferred", "normalised_name",
                f"{source} and {target} have the same name after normalisation. No fixture "
                "confirms that they carry the same value.", ())
    structural = [(source, decl) for source, decl in declared
                  if names.same_name(names.leaf(source), names.leaf(target))
                  and (source, decl) not in same_place]
    usable_structural: list[tuple[str, Decl]] = []
    for source, decl in structural:
        if decl.type == target_decl.type:
            usable_structural.append((source, decl))
        else:
            mismatched.append(f"{source} ({decl.type} against {target_decl.type})")
    if len(usable_structural) == 1 and not usable_same:
        source = usable_structural[0][0]
        return (source, "identity", "inferred", "structural_match",
                f"{source} and {target} have the same field name and type under different "
                "parents. No fixture confirms that they carry the same value.", ())
    if mismatched:
        out.notes[target] = ("a field with a matching name exists but its type differs and no "
                             "fixture establishes the conversion: " + ", ".join(sorted(set(
                                 mismatched))))
    elif len(usable_same) > 1 or len(usable_structural) > 1:
        out.notes[target] = "several fields have a matching name"
    return None
