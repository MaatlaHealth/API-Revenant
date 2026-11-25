"""The normalised contract model: both sides in one shape, built from stored facts.

Everything here is plain data. The engine works on these objects only, so it has
no database access and its output depends on nothing but the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from revenant.db.models import Endpoint, EndpointFact, EvidenceFile, Fixture
from revenant.evidence.service import current_files
from revenant.transform.model import SAFE_METHODS, is_name

CONTAINER_TYPES = ("object", "array")
Ref = dict[str, Any]


def fact_ref(fact: EndpointFact) -> Ref:
    """A reference that stays meaningful even if fact rows are rebuilt."""
    return {"type": "fact", "evidence_file_id": fact.evidence_file_id, "locator": fact.locator,
            "fact_type": fact.fact_type, "subject": fact.subject, "basis": fact.basis}


@dataclass(frozen=True)
class FieldInfo:
    path: str
    type: str
    required: bool  # required all the way down from the root
    enum: tuple[Any, ...] | None
    has_default: bool
    default: Any
    format: str | None
    ref: Ref

    @property
    def leaf(self) -> bool:
        return self.type not in CONTAINER_TYPES

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "type": self.type, "required": self.required}
        if self.enum is not None:
            out["enum"] = list(self.enum)
        if self.has_default:
            out["default"] = self.default
        if self.format:
            out["format"] = self.format
        return out


@dataclass(frozen=True)
class ParamInfo:
    location: str
    name: str
    type: str
    required: bool
    has_default: bool
    default: Any
    minimum: Any
    maximum: Any
    role: str | None  # pagination role, if the name is a known pagination parameter
    ref: Ref

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"in": self.location, "name": self.name, "type": self.type,
                               "required": self.required}
        if self.has_default:
            out["default"] = self.default
        if self.role:
            out["pagination_role"] = self.role
        return out


@dataclass(frozen=True)
class FixtureInfo:
    id: str
    name: str
    evidence_file_id: str
    request: dict[str, Any]
    response: dict[str, Any]
    pair_with: str | None
    variable_fields: tuple[str, ...]

    @property
    def status(self) -> int:
        return int(self.response["status"])

    @property
    def ref(self) -> Ref:
        return {"type": "fixture", "fixture_id": self.id, "name": self.name,
                "evidence_file_id": self.evidence_file_id}


@dataclass
class EndpointContract:
    id: str
    side: str
    method: str
    path: str
    operation_id: str | None
    summary: str
    declared: bool
    params: list[ParamInfo] = field(default_factory=list)
    request_fields: dict[str, FieldInfo] = field(default_factory=dict)
    responses: dict[str, dict[str, FieldInfo]] = field(default_factory=dict)
    statuses: dict[str, Ref] = field(default_factory=dict)  # declared status -> its fact
    auth: list[tuple[str, dict[str, Any], Ref]] = field(default_factory=list)
    effect_marker: tuple[str, Ref] | None = None
    unsupported: list[tuple[str, list[str], Ref]] = field(default_factory=list)
    fixtures: list[FixtureInfo] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def safe(self) -> bool:
        return self.method in SAFE_METHODS

    def path_params(self) -> list[str]:
        return [part[1:-1] for part in self.path.strip("/").split("/")
                if part.startswith("{") and part.endswith("}")]

    def success_fields(self) -> dict[str, FieldInfo]:
        """Declared fields of the 2xx responses, merged."""
        merged: dict[str, FieldInfo] = {}
        for status in sorted(self.responses):
            if status.startswith("2"):
                for path, info in self.responses[status].items():
                    merged.setdefault(path, info)
        return merged

    def describe(self) -> dict[str, Any]:
        """The contract as shown side by side in the comparison view."""
        return {
            "endpoint_id": self.id, "method": self.method, "path": self.path,
            "operation_id": self.operation_id, "summary": self.summary,
            "from_specification": self.declared,
            "parameters": [item.describe() for item in self.params],
            "request_fields": [info.describe() for _, info in sorted(self.request_fields.items())
                               if info.leaf],
            "responses": {status: [info.describe() for _, info in sorted(fields.items())
                                   if info.leaf]
                          for status, fields in sorted(self.responses.items())},
            "status_codes": sorted({*self.statuses, *(str(f.status) for f in self.fixtures)}),
            "auth": [{"scheme": name, **value} for name, value, _ in self.auth],
            "fixtures": len(self.fixtures),
        }


# ---- values -------------------------------------------------------------------------


def flatten_values(value: Any, prefix: str = "$") -> dict[str, list[Any]]:
    """Every scalar in a JSON value, by generalised path (``[]`` for array items).

    Values are listed in document order, so two arrays can be compared item by
    item. Keys that a pointer cannot address are left out.
    """
    out: dict[str, list[Any]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key in sorted(node):
                if is_name(str(key)):
                    walk(node[key], f"{path}.{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, f"{path}[]")
        else:
            out.setdefault(path, []).append(node)

    if value is not None:
        walk(value, prefix)
    return out


def template_values(template: str, path: str) -> dict[str, str] | None:
    """The values of ``{parameters}`` in a path, or None if it does not match."""
    expected, actual = template.strip("/").split("/"), path.strip("/").split("/")
    if len(expected) != len(actual):
        return None
    found: dict[str, str] = {}
    for part, value in zip(expected, actual, strict=True):
        if part.startswith("{") and part.endswith("}"):
            found[part[1:-1]] = value
        elif part != value:
            return None
    return found


def request_values(endpoint: EndpointContract, fixture: FixtureInfo) -> dict[str, list[Any]]:
    """Scoped scalars of a recorded request: ``body:``, ``query:`` and ``path:``."""
    out = {f"body:{path}": values
           for path, values in flatten_values(fixture.request.get("body")).items()}
    for name, value in sorted((fixture.request.get("query") or {}).items()):
        if is_name(str(name)) and not isinstance(value, dict | list):
            out[f"query:$.{name}"] = [value]
    for name, value in (template_values(endpoint.path, fixture.request["path"]) or {}).items():
        if is_name(name):
            out[f"path:$.{name}"] = [value]
    return out


def response_values(fixture: FixtureInfo) -> dict[str, list[Any]]:
    return flatten_values(fixture.response.get("body"))


# ---- loading ------------------------------------------------------------------------


def _parents(path: str) -> list[str]:
    found: list[str] = []
    while path != "$":
        path = path[:-2] if path.endswith("[]") else path.rsplit(".", 1)[0]
        found.append(path)
    return found


def _fields(facts: list[EndpointFact], scope: str) -> dict[str, FieldInfo]:
    """Field facts of one scope (``request`` or ``response[201]``) as a path map."""
    raw: dict[str, EndpointFact] = {}
    for fact in facts:
        prefix, _, path = fact.subject.partition(":")
        if prefix == scope and path not in raw:
            raw[path] = fact
    fields: dict[str, FieldInfo] = {}
    for path, fact in raw.items():
        value = fact.value
        required = bool(value.get("required", False)) and all(
            bool(raw[parent].value.get("required", True)) for parent in _parents(path)
            if parent in raw)
        enum = value.get("enum")
        fields[path] = FieldInfo(
            path=path, type=str(value.get("type", "any")), required=required,
            enum=tuple(enum) if isinstance(enum, list) else None,
            has_default="default" in value, default=value.get("default"),
            format=value.get("format"), ref=fact_ref(fact),
        )
    return fields


def load_contracts(session: Session, project_id: str) -> tuple[list[EndpointContract],
                                                             list[EvidenceFile]]:
    """The current evidence of a project as contracts, in a deterministic order."""
    files = current_files(session, project_id)
    order = {item.id: (item.sha256, item.filename) for item in files}
    endpoints = list(session.scalars(
        select(Endpoint).where(Endpoint.project_id == project_id)
        .order_by(Endpoint.side, Endpoint.path_template, Endpoint.method)))
    facts_by_endpoint: dict[str, list[EndpointFact]] = {}
    if order and endpoints:
        for fact in session.scalars(select(EndpointFact).where(
                EndpointFact.evidence_file_id.in_(list(order)),
                EndpointFact.endpoint_id.in_([item.id for item in endpoints]))):
            facts_by_endpoint.setdefault(fact.endpoint_id, []).append(fact)
    fixtures_by_endpoint: dict[str, list[FixtureInfo]] = {}
    for row in session.scalars(select(Fixture).where(Fixture.project_id == project_id)
                               .order_by(Fixture.name, Fixture.content_sha256)):
        if row.evidence_file_id in order and row.endpoint_id:
            fixtures_by_endpoint.setdefault(row.endpoint_id, []).append(FixtureInfo(
                id=row.id, name=row.name, evidence_file_id=row.evidence_file_id,
                request=row.request, response=row.expected_response, pair_with=row.pair_with,
                variable_fields=tuple(row.normalisation.get("variable_fields", [])),
            ))

    contracts: list[EndpointContract] = []
    for endpoint in endpoints:
        facts = sorted(
            facts_by_endpoint.get(endpoint.id, []),
            key=lambda f: (f.fact_type, f.subject, f.basis, order[f.evidence_file_id], f.locator))
        if not facts:
            continue  # nothing in the current evidence supports this endpoint
        declared = [fact for fact in facts if fact.basis == "declared"]
        contract = EndpointContract(
            id=endpoint.id, side=endpoint.side, method=endpoint.method,
            path=endpoint.path_template, operation_id=endpoint.operation_id,
            summary=endpoint.summary, declared=bool(declared),
            fixtures=fixtures_by_endpoint.get(endpoint.id, []),
        )
        field_facts = [fact for fact in declared if fact.fact_type == "field"]
        contract.request_fields = _fields(field_facts, "request")
        roles = {fact.subject: str(fact.value.get("role")) for fact in declared
                 if fact.fact_type == "pagination"}
        seen: set[str] = set()
        for fact in declared:
            if fact.fact_type in ("parameter", "header") and ":" in fact.subject:
                location, _, name = fact.subject.partition(":")
                if location.startswith("response[") or fact.subject in seen:
                    continue
                seen.add(fact.subject)
                value = fact.value
                contract.params.append(ParamInfo(
                    location=location, name=name, type=str(value.get("type", "any")),
                    required=bool(value.get("required", False)),
                    has_default="default" in value, default=value.get("default"),
                    minimum=value.get("minimum"), maximum=value.get("maximum"),
                    role=roles.get(name) if location == "query" else None, ref=fact_ref(fact)))
            elif fact.fact_type == "status_code" and fact.subject not in contract.statuses:
                contract.statuses[fact.subject] = fact_ref(fact)
                fields = _fields(field_facts, f"response[{fact.subject}]")
                if fields:
                    contract.responses[fact.subject] = fields
            elif fact.fact_type == "auth" and fact.subject != "none":
                if fact.subject not in [name for name, _, _ in contract.auth]:
                    contract.auth.append((fact.subject, dict(fact.value), fact_ref(fact)))
            elif fact.fact_type == "effect" and contract.effect_marker is None:
                contract.effect_marker = (str(fact.value.get("effect")), fact_ref(fact))
            elif fact.fact_type == "unsupported_content":
                contract.unsupported.append(
                    (fact.subject, list(fact.value.get("media_types", [])), fact_ref(fact)))
        contracts.append(contract)
    return contracts, files
