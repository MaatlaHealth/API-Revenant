"""The reconstruction engine: contracts in, proposals, gaps, invariants and the
behaviour inventory out. Pure and deterministic: the same evidence gives the
same result, in the same order."""

from __future__ import annotations

from collections import Counter
from typing import Any

from revenant.db.models import EvidenceFile
from revenant.reconstruction import invariants, inventory, pairing, proposals
from revenant.reconstruction.contract import EndpointContract
from revenant.reconstruction.types import Result


def reconstruct(contracts: list[EndpointContract], files: list[EvidenceFile]) -> Result:
    legacy = [item for item in contracts if item.side == "legacy"]
    replacement = [item for item in contracts if item.side == "replacement"]
    pairs, unpaired = pairing.pair_endpoints(legacy, replacement)
    analysis = proposals.analyse(legacy, replacement, pairs, unpaired)
    derived = invariants.derive(legacy)
    behaviours = inventory.enumerate_behaviours(legacy, analysis.gaps, files)

    paired_replacements = {pair.replacement.id for pair in pairs}
    report: dict[str, Any] = {
        "pairs": [{
            "legacy": pair.legacy.describe(), "replacement": pair.replacement.describe(),
            "confidence": pair.confidence, "score": pair.score,
            "paired_recordings": len(pair.fixture_pairs),
            "differences": analysis.differences.get(pair.legacy.id, []),
        } for pair in pairs],
        "unpaired_legacy": [{"legacy": endpoint.describe(), "reason": reason}
                            for endpoint, reason in unpaired],
        "unpaired_replacement": [endpoint.describe() for endpoint in replacement
                                 if endpoint.id not in paired_replacements],
        "project": analysis.differences.get("project", []),
    }
    by_category: dict[str, dict[str, int]] = {}
    for proposal in analysis.proposals:
        by_category.setdefault(proposal.category, {"observed": 0, "inferred": 0})[
            proposal.confidence] += 1
    summary: dict[str, Any] = {
        "endpoints": {"legacy": len(legacy), "replacement": len(replacement),
                      "paired": len(pairs), "unpaired_legacy": len(unpaired)},
        "mappings": {"total": len(analysis.proposals),
                     **Counter(p.confidence for p in analysis.proposals),
                     "by_category": dict(sorted(by_category.items()))},
        "gaps": {"total": len(analysis.gaps),
                 **Counter(gap.severity for gap in analysis.gaps),
                 "by_category": dict(sorted(Counter(g.category for g in analysis.gaps).items()))},
        "invariants": {"total": len(derived),
                       **Counter(item.confidence for item in derived),
                       "by_kind": dict(sorted(Counter(i.kind for i in derived).items()))},
        "behaviours": {"total": len(behaviours),
                       "by_kind": dict(sorted(Counter(b.kind for b in behaviours).items())),
                       "with_evidence": sum(1 for b in behaviours if b.evidence_refs
                                            and b.kind != "unsupported"),
                       "project_level": sum(1 for b in behaviours if b.endpoint_ref is None)},
    }
    for block in ("mappings", "invariants"):
        summary[block].setdefault("observed", 0)
        summary[block].setdefault("inferred", 0)
    for severity in ("blocking", "advisory"):
        summary["gaps"].setdefault(severity, 0)
    return Result(proposals=analysis.proposals, gaps=analysis.gaps, invariants=derived,
                  behaviours=behaviours, report=report, summary=summary)
