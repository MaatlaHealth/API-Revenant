"""Mutation check for reconstruction and mapping review.

Run from the ``backend`` directory:

    python tests/mutation/reconstruction_mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import Mutations, run  # noqa: E402

TESTS = ["tests/unit/test_reconstruction_engine.py",
         "tests/integration/test_reconstruction_api.py"]

SERVICE = "revenant/reconstruction/service.py"
EFFECTS = "revenant/reconstruction/effects.py"
PROPOSALS = "revenant/reconstruction/proposals.py"
FIELDS = "revenant/reconstruction/fields.py"
CORRELATE = "revenant/reconstruction/correlate.py"
PAIRING = "revenant/reconstruction/pairing.py"
PAGINATION = "revenant/reconstruction/pagination.py"
INVARIANTS = "revenant/reconstruction/invariants.py"
INVENTORY = "revenant/reconstruction/inventory.py"
TYPES = "revenant/reconstruction/types.py"
SHAPES = "revenant/reconstruction/shapes.py"
MODEL = "revenant/transform/model.py"
API = "revenant/api/reconstruction.py"

MUTATIONS: Mutations = {
    # ---- operation effect ---------------------------------------------------------------
    "propose read-only for safe methods": (
        PROPOSALS, '        effect, confidence, rule = "unknown", "inferred", '
                   '"safe_methods_prove_nothing"',
        '        effect, confidence, rule = "read_only", "inferred", '
        '"safe_methods_prove_nothing"',
    ),
    "ignore the state-changing marker in a specification": (
        PROPOSALS, "    elif marked:", "    elif False:",
    ),
    "drop the method floor": (
        EFFECTS,
        "    if legacy_method.upper() not in SAFE_METHODS or replacement_method.upper() "
        "not in SAFE_METHODS:\n        return \"state_changing\"\n",
        "",
    ),
    "treat no declaration as read-only": (
        EFFECTS, '    return "unknown"\n', '    return "read_only"\n',
    ),
    "allow read-only on an unsafe method": (
        EFFECTS, "                if method.upper() not in SAFE_METHODS]",
        "                if False]",
    ),
    "allow read-only against an evidence marker": (
        EFFECTS, '                 for name, effect in markers if effect == "state_changing"]',
        "                 for name, effect in markers if False]",
    ),
    "do not check a read-only declaration": (
        SERVICE, '        if effect == "read_only":\n            self._check_read_only(project, '
                 "route)\n", "",
    ),
    "accept an effect declaration without a note": (
        SERVICE, "        if not note.strip():\n            raise ValidationFailed(\"A note is "
                 "required when declaring", "        if False:\n            raise "
                 "ValidationFailed(\"A note is required when declaring",
    ),
    "do not audit effect declarations": (
        SERVICE, 'action="operation.effect.declare"', 'action="mapping.approve"',
    ),
    # ---- review ---------------------------------------------------------------------------
    "bulk-approve inferred mappings by filter": (
        SERVICE, '                    if row.confidence == "observed" and not '
                 "row.requires_approval]", "                    if True]",
    ),
    "do not record decisions": (
        SERVICE, "        mapping.status = decision\n        self._decide(mapping, decision, "
                 "actor, note)\n", "        mapping.status = decision\n",
    ),
    "store an edit without validating it": (
        SERVICE, "        problems = validate_transformation(mapping.category, transformation)",
        "        problems: list[str] = []",
    ),
    "overwrite the proposal instead of superseding it": (
        SERVICE, '        mapping.status = "superseded"\n        self._decide(mapping, "edited", '
                 "actor, note)\n", "",
    ),
    "present an edit as evidence-backed": (
        SERVICE, '            confidence="inferred", rule_name="human_edit",',
        '            confidence="observed", rule_name="human_edit",',
    ),
    "allow review of earlier reconstructions": (
        SERVICE, "        if latest is None or latest.id != reconstruction_id:",
        "        if False:",
    ),
    "let an edit change the legacy route": (
        SERVICE, "            if legacy is None or (match[\"method\"], match[\"path\"]) != (\n"
                 "                    legacy.method, legacy.path_template):",
        "            if False:",
    ),
    "let rules read credential headers": (
        MODEL, '        if scope == "header" and pointer[2:].lower() in CREDENTIAL_HEADERS:\n'
               '            problems.append(f"{source} is a credential carrier and cannot be '
               'read by a rule")', "        pass",
    ),
    "let rules write credential headers": (
        MODEL, '        if target_scope == "header" and target[2:].lower() in '
               "CREDENTIAL_HEADERS:", "        if False:",
    ),
    "accept unknown conversions": (
        MODEL, "        if value not in CONVERSIONS:", "        if False:",
    ),
    "accept rules that read unavailable scopes": (
        MODEL, "        if scope not in scopes:", "        if False:",
    ),
    "do not check mappings against the project": (
        SERVICE, '        if row is None or row.project_id != project_id:\n            raise '
                 'NotFound("Mapping not found.")',
        '        if row is None:\n            raise NotFound("Mapping not found.")',
    ),
    "allow a person to declare a gap resolved": (
        API, '    resolution: Literal["open", "accepted_risk"]',
        '    resolution: Literal["open", "accepted_risk", "resolved_by_evidence"]',
    ),
    "mark a behaviour out of scope without a note": (
        SERVICE, '        if scope == "out_of_scope" and not note.strip():', "        if False:",
    ),
    # ---- staleness and inventories --------------------------------------------------------
    "never report a reconstruction as stale": (
        SERVICE, "        return record.evidence_fingerprint != project.evidence_fingerprint",
        "        return False",
    ),
    "do not report the inventory difference": (
        INVENTORY, '            "added": sorted(after - before), "removed": sorted(before - '
                   "after)}", '            "added": [], "removed": []}',
    ),
    "leave undocumented status codes out of the inventory": (
        INVENTORY, "        statuses = {status for status in endpoint.statuses if "
                   "status.isdigit()}", "        statuses: set[str] = set()",
    ),
    "leave gaps out of the inventory": (
        INVENTORY, "        if gap.behaviour_key in known:", "        if True:",
    ),
    "leave unreadable evidence out of the inventory": (
        INVENTORY, '        if item.side == "legacy" and item.parse_status in ("failed", '
                   '"unsupported"):', "        if False:",
    ),
    # ---- proposers ------------------------------------------------------------------------
    "classify inferred mappings as not needing approval": (
        TYPES, '        return self.confidence == "inferred"', "        return False",
    ),
    "classify name matches as observed": (
        FIELDS, '        return (source, "identity", "inferred", "normalised_name",',
        '        return (source, "identity", "observed", "normalised_name",',
    ),
    "use name matches even when fixtures show the field": (
        FIELDS, "            elif target_decl is not None and target not in seen_targets:",
        "            elif target_decl is not None:",
    ),
    "pick the first of several correlated fields": (
        FIELDS, "        if len(found) == 1:\n            item = found[0]",
        "        if len(found) >= 1:\n            item = found[0]",
    ),
    "accept a correlation that fails in some recordings": (
        CORRELATE, "                    consistent = False\n                    break\n"
                   "                confirming.append(index)",
        "                    continue\n                confirming.append(index)",
    ),
    "count booleans and nulls as evidence": (
        CORRELATE, '    return not (value is None or isinstance(value, bool) or value == "")',
        "    return True",
    ),
    "accept inconsistent enumeration mappings": (
        CORRELATE, "            if mapping.setdefault(a, b) != b:\n                return None",
        "            mapping.setdefault(a, b)",
    ),
    "present borrowed enumeration values as observed": (
        PROPOSALS, '        if notes:\n            proposal.confidence = "inferred"',
        "        if False:\n            proposal.confidence = \"inferred\"",
    ),
    "force a pair below the threshold": (
        PAIRING, "        if value >= THRESHOLD:", "        if True:",
    ),
    "break ties between endpoints by order": (
        PAIRING, "        if len(top) > 1:", "        if False:",
    ),
    "classify similarity pairs as observed": (
        PAIRING, '                                "inferred", value, [])',
        '                                "observed", value, [])',
    ),
    "propose pagination from one page as observed": (
        PAGINATION, "    all_observed = len(pages) >= 2", "    all_observed = True",
    ),
    "guess an ambiguous cursor": (
        PAGINATION, "        return isinstance(options, set) and len(options) == 1",
        "        return isinstance(options, set) and len(options) >= 1",
    ),
    "claim termination from one polarity": (
        PAGINATION, "            if seen == {True, False}:", "            if True:",
    ),
    "emit a mapping for required fields nothing supplies": (
        PROPOSALS, "    if needed:\n", "    if False:\n",
    ),
    "do not report unmapped legacy response fields": (
        PROPOSALS, "    if absent:\n", "    if False:\n",
    ),
    # ---- invariants -----------------------------------------------------------------------
    "activate inferred invariants without approval": (
        SERVICE, '                status="active" if invariant.confidence == "observed" else '
                 '"proposed"))', '                status="active"))',
    ),
    "classify single-fixture echo invariants as observed": (
        INVARIANTS, '                confidence="observed" if len(values) >= 2 and distinct >= 2 '
                    'else "inferred",', '                confidence="observed",',
    ),
    "derive invariants from the replacement side": (
        INVARIANTS, '        if endpoint.side != "legacy":', "        if False:",
    ),
    "keep a pagination invariant the fixtures contradict": (
        INVARIANTS, "    if not holds:\n        return []  # the fixtures contradict it",
        "    if False:\n        return []  # the fixtures contradict it",
    ),
}

if __name__ == "__main__":
    sys.exit(run(MUTATIONS, TESTS))
