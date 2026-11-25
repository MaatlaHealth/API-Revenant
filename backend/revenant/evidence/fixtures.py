"""Historical fixture files (docs/api-contracts.md, "Fixture file format").

Each fixture is validated on its own: a bad one is rejected with a reason and
the rest are kept. Credential values are redacted at import and are not stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from revenant.security.redaction import REDACTED, SENSITIVE_HEADERS

SUPPORTED_VERSIONS = ("1.0",)
MAX_FIXTURES = 2000
_METHODS = {"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS"}
# Header and query-parameter names that carry credentials even when they are not
# one of the standard headers.
_CREDENTIAL_NAME = re.compile(
    r"(api[-_]?key|apikey|token|secret|password|passwd|signature|credential|session|auth)",
    re.IGNORECASE,
)


@dataclass
class ParsedFixture:
    name: str
    request: dict[str, Any]
    response: dict[str, Any]
    recorded_at: str | None
    variable_fields: list[str]
    pair_with: str | None
    content_sha256: str


@dataclass
class ParsedFixtureFile:
    fixtures: list[ParsedFixture] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    redactions: int = 0
    redacted_document: dict[str, Any] = field(default_factory=dict)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_credential_name(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADERS or bool(_CREDENTIAL_NAME.search(name))


def _redact_mapping(mapping: Any, counter: list[int]) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for name, value in mapping.items():
        if is_credential_name(str(name)) and value != REDACTED:
            cleaned[str(name)] = REDACTED
            counter[0] += 1
        else:
            cleaned[str(name)] = value
    return cleaned


def _validate(index: int, raw: Any, seen: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    label = f"fixture {index + 1}"
    if not isinstance(raw, dict):
        return None, f"{label}: must be an object"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, f"{label}: 'name' is required"
    label = f"fixture {name!r}"
    if len(name) > 300:
        return None, f"fixture {index + 1}: 'name' is longer than 300 characters"
    if name in seen:
        return None, f"{label}: duplicate name in this file"
    request, response = raw.get("request"), raw.get("response")
    if not isinstance(request, dict):
        return None, f"{label}: 'request' must be an object"
    if not isinstance(response, dict):
        return None, f"{label}: 'response' must be an object"
    method = str(request.get("method") or "").upper()
    if method not in _METHODS:
        return None, f"{label}: request.method must be an HTTP method"
    path = request.get("path")
    if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
        return None, f"{label}: request.path must start with '/' and contain no query string"
    status = response.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None, f"{label}: response.status must be an HTTP status code"
    for part, owner in (("query", request), ("headers", request), ("headers", response)):
        if part in owner and not isinstance(owner[part], dict):
            return None, f"{label}: {part} must be an object"
    variable = raw.get("variable_fields", [])
    if not isinstance(variable, list) or not all(isinstance(item, str) for item in variable):
        return None, f"{label}: variable_fields must be a list of paths"
    pair = raw.get("pair_with")
    if pair is not None and not isinstance(pair, str):
        return None, f"{label}: pair_with must be the name of a legacy fixture"
    return raw, None


def parse_fixture_file(document: Any) -> ParsedFixtureFile:
    """Validate a fixture document. Raises ``ValueError`` if the file itself is unusable."""
    if not isinstance(document, dict):
        raise ValueError("a fixture file must be an object")
    version = str(document.get("revenant_fixtures"))
    if version not in SUPPORTED_VERSIONS:
        supported = ", ".join(SUPPORTED_VERSIONS)
        raise ValueError(f"unsupported fixture format version {version!r}; supported: {supported}")
    items = document.get("fixtures")
    if not isinstance(items, list):
        raise ValueError("'fixtures' must be a list")
    if len(items) > MAX_FIXTURES:
        raise ValueError(f"more than {MAX_FIXTURES} fixtures in one file")

    result = ParsedFixtureFile()
    counter = [0]
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        valid, problem = _validate(index, raw, seen)
        if valid is None:
            result.rejected.append({"index": str(index), "reason": problem or "invalid"})
            continue
        seen.add(valid["name"])
        request = {
            "method": str(valid["request"]["method"]).upper(),
            "path": valid["request"]["path"],
            "query": _redact_mapping(valid["request"].get("query"), counter),
            "headers": _redact_mapping(valid["request"].get("headers"), counter),
            "body": valid["request"].get("body"),
        }
        response = {
            "status": valid["response"]["status"],
            "headers": _redact_mapping(valid["response"].get("headers"), counter),
            "body": valid["response"].get("body"),
        }
        recorded = valid.get("recorded_at")
        cleaned: dict[str, Any] = {"name": valid["name"], "request": request, "response": response}
        if isinstance(recorded, str):
            cleaned["recorded_at"] = recorded[:40]
        if valid.get("variable_fields"):
            cleaned["variable_fields"] = list(valid["variable_fields"])
        if valid.get("pair_with"):
            cleaned["pair_with"] = valid["pair_with"]
        kept.append(cleaned)
        result.fixtures.append(ParsedFixture(
            name=valid["name"], request=request, response=response,
            recorded_at=cleaned.get("recorded_at"),
            variable_fields=list(valid.get("variable_fields") or []),
            pair_with=valid.get("pair_with"),
            content_sha256=hashlib.sha256(
                canonical({"request": request, "response": response}).encode("utf-8")
            ).hexdigest(),
        ))
    result.redactions = counter[0]
    result.redacted_document = {"revenant_fixtures": version, "fixtures": kept}
    return result


def template_score(template: str, path: str) -> int | None:
    """How well a path matches a template; None if it does not match.

    The score is the number of literal segments, so ``/charges/search`` prefers
    a literal template over ``/charges/{id}``.
    """
    template_parts = template.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(template_parts) != len(path_parts):
        return None
    score = 0
    for expected, actual in zip(template_parts, path_parts, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            if not actual:
                return None
        elif expected == actual:
            score += 1
        else:
            return None
    return score
