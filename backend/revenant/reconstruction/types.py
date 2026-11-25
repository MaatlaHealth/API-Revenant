"""What the engine produces. Plain data, in a deterministic order."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Ref = dict[str, Any]


@dataclass
class Proposal:
    key: str
    category: str
    legacy_endpoint_id: str | None
    replacement_endpoint_id: str | None
    legacy_ref: str
    replacement_ref: str
    transformation: dict[str, Any]
    confidence: str  # observed | inferred
    rule_name: str
    rationale: str
    evidence_refs: list[Ref] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        """Inferred mappings must be approved one by one; they are never bulk-approved."""
        return self.confidence == "inferred"


@dataclass
class GapOut:
    key: str
    category: str
    endpoint_id: str | None
    endpoint_ref: str | None
    title: str
    description: str
    missing: str
    severity: str  # blocking | advisory
    evidence_refs: list[Ref] = field(default_factory=list)
    # An inventory behaviour that already stands for this gap; otherwise it gets its own.
    behaviour_key: str | None = None


@dataclass
class InvariantOut:
    key: str
    endpoint_id: str
    endpoint_ref: str
    kind: str
    definition: dict[str, Any]
    confidence: str
    evidence_refs: list[Ref] = field(default_factory=list)


@dataclass
class BehaviourOut:
    key: str
    kind: str
    endpoint_id: str | None
    endpoint_ref: str | None
    description: str
    evidence_refs: list[Ref] = field(default_factory=list)
    unsupported_reason: str | None = None


@dataclass
class Result:
    proposals: list[Proposal]
    gaps: list[GapOut]
    invariants: list[InvariantOut]
    behaviours: list[BehaviourOut]
    report: dict[str, Any]
    summary: dict[str, Any]


def response_key(endpoint_name: str, status: str | int) -> str:
    return f"{endpoint_name} → {status}"


def dedupe(refs: list[Ref]) -> list[Ref]:
    seen: set[str] = set()
    out: list[Ref] = []
    for ref in refs:
        marker = repr(sorted(ref.items()))
        if marker not in seen:
            seen.add(marker)
            out.append(ref)
    return out
