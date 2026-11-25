"""Mutation check for the transformation engine, the validator and the compiler.

Run from the ``backend`` directory:

    python tests/mutation/adapter_mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import Mutations, run  # noqa: E402

TESTS = ["tests/unit/test_transform_engine.py", "tests/property/test_engine_properties.py",
         "tests/integration/test_adapters_api.py"]

ENGINE = "revenant/transform/engine.py"
VALIDATOR = "revenant/adapter/validator.py"
COMPILER = "revenant/adapter/compiler.py"

MUTATIONS: Mutations = {
    # ---- what may be compiled ---------------------------------------------------------------
    "compile mappings that are not approved": (
        COMPILER, '        approved = self.reconstructions.mappings(latest.id, status="approved")',
        "        approved = self.reconstructions.mappings(latest.id)",
    ),
    "compile from a stale reconstruction": (
        COMPILER, "        if self.reconstructions.is_stale(project, latest):", "        if False:",
    ),
    "emit a route whose required request fields are missing": (
        COMPILER, "            if missing:\n                reasons.append(\"required "
                  "replacement request fields",
        "            if False:\n                reasons.append(\"required replacement request "
        "fields",
    ),
    "emit a route whose required response fields are missing": (
        COMPILER, "            if absent:", "            if False:",
    ),
    "emit a paginated route without pagination": (
        COMPILER, "            if paginated and not paging:", "            if False:",
    ),
    "store a document that failed static validation": (
        COMPILER, "        if problems:\n            raise AdapterInvalid(\"The compiled adapter "
                  "failed static validation.\"",
        "        if False:\n            raise AdapterInvalid(\"The compiled adapter failed "
        "static validation.\"",
    ),
    "default an undeclared safe route to read-only": (
        COMPILER, '            "effect": "unknown" if {match["method"], upstream["method"]} <= '
                  "SAFE_METHODS", '            "effect": "read_only" if {match["method"], '
                                  'upstream["method"]} <= SAFE_METHODS',
    ),
    "overwrite an existing adapter version": (
        COMPILER, "            .where(AdapterVersion.project_id == project.id)) or 0) + 1",
        "            .where(AdapterVersion.project_id == project.id)) or 0) or 1",
    ),
    "serve an adapter to another project": (
        COMPILER, "            AdapterVersion.project_id == project_id, AdapterVersion.version "
                  "== version))", "            AdapterVersion.version == version))",
    ),
    # ---- credentials ------------------------------------------------------------------------
    "leave the legacy key in the headers the rules see": (
        COMPILER, "        if lowered not in read_headers and lowered not in CREDENTIAL_HEADERS:",
        "        if True:",
    ),
    "do not block a legacy key that reaches the upstream request": (
        COMPILER, "        if any(value and value in json.dumps(produced) for value in "
                  "presented):", "        if False:",
    ),
    "let rules read the declared inbound carrier": (
        VALIDATOR, '                if scope == "header" and name in self.read_headers:',
        "                if False:",
    ),
    "let rules read a query credential": (
        VALIDATOR, '                if scope in ("query", "request.query") and name in '
                   "self.read_query:", "                if False:",
    ),
    "let rules write a credential header": (
        VALIDATOR, '                if target_scope == "header" and name in '
                   "self.written_headers:", "                if False:",
    ),
    "let rules write a query credential": (
        VALIDATOR, '                if target_scope == "query" and name in self.written_query | '
                   "self.read_query:", "                if False:",
    ),
    "let pagination write a credential parameter": (
        VALIDATOR, "            if written and written.lower() in self.written_query | "
                   "self.read_query:", "            if False:",
    ),
    # ---- static validation --------------------------------------------------------------------
    "accept rules without a mapping": (
        VALIDATOR, "        if not mapping_id:", "        if False:",
    ),
    "accept rules from unapproved mappings": (
        VALIDATOR, "              and mapping_id not in self.context.approved_mapping_ids):",
        "              and False):",
    ),
    "accept read-only on an unsafe method": (
        VALIDATOR, "            if unsafe:", "            if False:",
    ),
    "accept read-only without an approved effect mapping": (
        VALIDATOR, '            self.mapping(f"{where} effect", route.effect_mapping_id)',
        "            pass",
    ),
    "accept two rules writing one target": (
        VALIDATOR, "                if target in seen:", "                if False:",
    ),
    "accept a missing path parameter": (
        VALIDATOR, "            if name not in produced:", "            if False:",
    ),
    "do not check required request fields": (
        VALIDATOR, '        if missing and route.request.body.mode == "build":',
        "        if False:",
    ),
    "accept an unauthenticated runtime without the setting": (
        VALIDATOR, '        if document.auth.inbound.type == "none" and not '
                   "context.allow_unauthenticated:", "        if False:",
    ),
    "ignore a rejected base URL": (
        VALIDATOR, "        if context.base_url_problem:", "        if False:",
    ),
    # ---- engine semantics -----------------------------------------------------------------------
    "ignore conditions": (
        ENGINE, "            if not holds(rule.when, scopes):", "            if False:",
    ),
    "skip a required source silently": (
        ENGINE, "        if rule.required:", "        if False:",
    ),
    "pass unmapped enumeration values through": (
        ENGINE, '        elif rule.on_unmapped == "error":', "        elif False:",
    ),
    "round instead of failing on precision loss": (
        ENGINE, "    if Decimal(repr(as_float)) != result:", "    if False:",
    ),
    "truncate fractions to integers": (
        ENGINE, "        if not isinstance(result, int):\n            raise TransformError("
                "\"precision_loss\", \"the value is not a whole number\")\n        return result",
        "        return int(result)",
    ),
    "drop milliseconds silently": (
        ENGINE, "        if _whole(value) % 1000:", "        if False:",
    ),
    "let default overwrite a written value": (
        ENGINE, "        if get(result.output, rule.to) is not MISSING:", "        if False:",
    ),
    "start the output from the source": (
        ENGINE, "    result = Applied(output=start)",
        "    result = Applied(output=copy.deepcopy(scopes.get(\"body\")) if start is MISSING "
        "and isinstance(scopes.get(\"body\"), dict) else start)",
    ),
    "share objects between source and output": (
        ENGINE, "        result.output = put(result.output, rule.to, "
                "copy.deepcopy(_check(value)))",
        "        result.output = put(result.output, rule.to, _check(value))",
    ),
    "expand placeholders inside substituted values": (
        ENGINE, "        text = _PLACEHOLDER.sub(lambda match: values[match.group(1)], "
                "rule.template)",
        "        text = rule.template\n        for _name in list(values) * 2:\n"
        "            text = text.replace(\"{\" + _name + \"}\", values[_name])",
    ),
    "do not percent-encode path values": (
        ENGINE, '        return quote(convert(values[name], "string", {}), safe="")',
        '        return convert(values[name], "string", {})',
    ),
    "accept any page number": (
        ENGINE, "    if page < legacy.first_page:", "    if False:",
    ),
    "ignore the maximum page size": (
        ENGINE, "    if limit is not None and (limit < 1 or (legacy.max_limit and limit > "
                "legacy.max_limit)):", "    if limit is not None and limit < 1:",
    ),
    "compute the offset from page zero": (
        ENGINE, '              "$offset": (page - first) * limit if limit is not None else '
                "None}", '              "$offset": page * limit if limit is not None else None}',
    ),
    "invent a body for an unmapped error": (
        ENGINE, "    return LegacyResponse(status=status, headers={}, body=None,\n"
                "                          trace={\"errors\": [], \"envelope\": []}, "
                "unmapped_error=True)",
        "    return LegacyResponse(status=status, headers={}, body={\"error\": {\"code\": "
        "\"unknown\"}},\n                          trace={\"errors\": [], \"envelope\": []}, "
        "unmapped_error=False)",
    ),
    "report has_more for a null marker": (
        ENGINE, "marker is not MISSING and marker is not None)", "marker is not MISSING)",
    ),
    "put values in traces": (
        ENGINE, '            "from": getattr(rule, "from_", None),',
        '            "from": str(read(scopes, rule.from_)) if hasattr(rule, "from_") else None,',
    ),
    "do not report unmapped source fields": (
        ENGINE, "    return sorted(path for path in found if not any(", "    return sorted(path "
                "for path in () if not any(",
    ),
    "skip the array size limit": (
        ENGINE, "        if len(items) > MAX_ARRAY:", "        if False:",
    ),
}

if __name__ == "__main__":
    sys.exit(run(MUTATIONS, TESTS))
