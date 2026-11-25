"""Evidence Vault endpoints (docs/api-contracts.md, "Evidence")."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from revenant import audit
from revenant.api.deps import AdminActor, CurrentProject, DbSession, get_settings, require_admin
from revenant.api.projects import evidence_service
from revenant.config import Settings
from revenant.db.models import Endpoint, EvidenceFile, Fixture
from revenant.errors import NotFound, PayloadTooLarge, UnsupportedEvidence
from revenant.evidence.loader import EvidenceRejected

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["evidence"],
                   dependencies=[Depends(require_admin)])

Side = Literal["legacy", "replacement"]
Kind = Literal["openapi", "swagger", "json_schema", "fixtures", "markdown"]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class EvidenceOut(BaseModel):
    id: str
    side: str
    kind: str
    filename: str
    size_bytes: int
    sha256: str
    parse_status: str
    parse_report: dict[str, Any]
    fact_count: int
    created_at: datetime


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualRequest(_Body):
    method: str
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


class ManualResponse(_Body):
    status: int
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


class ManualExample(_Body):
    name: str = Field(min_length=1, max_length=300)
    side: Side = "legacy"
    request: ManualRequest
    response: ManualResponse
    variable_fields: list[str] = Field(default_factory=list)
    pair_with: str | None = None


def _evidence(row: EvidenceFile, fact_count: int) -> EvidenceOut:
    return EvidenceOut(
        id=row.id, side=row.side, kind=row.kind, filename=row.filename,
        size_bytes=row.size_bytes, sha256=row.sha256, parse_status=row.parse_status,
        parse_report=row.parse_report, fact_count=fact_count, created_at=row.created_at,
    )


def _endpoint(row: Endpoint, detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id, "side": row.side, "method": row.method,
        "path_template": row.path_template, "operation_id": row.operation_id,
        "summary": row.summary, "from_specification": row.api_spec_id is not None,
    }
    if detail:
        out.update(parameters=row.parameters, request_schema=row.request_schema,
                   response_schemas=row.response_schemas)
    return out


def _fixture(row: Fixture) -> dict[str, Any]:
    return {
        "id": row.id, "side": row.side, "name": row.name, "endpoint_id": row.endpoint_id,
        "evidence_file_id": row.evidence_file_id, "request": row.request,
        "expected_response": row.expected_response, "recorded_at": row.recorded_at,
        "variable_fields": row.normalisation.get("variable_fields", []),
        "pair_with_name": row.pair_with_name, "pair_with": row.pair_with,
        "content_sha256": row.content_sha256,
    }


def _ingest(project: CurrentProject, session: DbSession, settings: Settings, actor: str,
            filename: str | None, content_type: str | None, raw: bytes, side: str,
            kind: str | None) -> EvidenceOut:
    service = evidence_service(session, settings)
    try:
        record = service.ingest(project, filename, content_type, raw, side, kind)
    except EvidenceRejected as exc:
        if exc.code == "payload_too_large":
            raise PayloadTooLarge(exc.message) from exc
        raise UnsupportedEvidence(exc.message) from exc
    audit.record(session, actor=actor, action="evidence.import", target_type="evidence_file",
                 target_id=record.id, project_id=project.id,
                 meta={"filename": record.filename, "side": side, "kind": record.kind,
                       "parse_status": record.parse_status,
                       "evidence_version": project.evidence_version})
    return _evidence(record, service.fact_count(record.id))


@router.post("/evidence", status_code=201)
def upload_evidence(
    project: CurrentProject, session: DbSession, settings: SettingsDep, actor: AdminActor,
    file: Annotated[UploadFile, File()],
    side: Annotated[Side, Form()],
    kind: Annotated[Kind | None, Form()] = None,
) -> EvidenceOut:
    # Read one byte more than the limit so an oversized upload is detected
    # without holding the whole of it in memory.
    raw = file.file.read(settings.max_upload_bytes + 1)
    return _ingest(project, session, settings, actor, file.filename, file.content_type, raw,
                   side, kind)


@router.post("/evidence/manual", status_code=201)
def add_manual_example(body: ManualExample, project: CurrentProject, session: DbSession,
                       settings: SettingsDep, actor: AdminActor) -> EvidenceOut:
    fixture: dict[str, Any] = {
        "name": body.name,
        "request": body.request.model_dump(),
        "response": body.response.model_dump(),
        "variable_fields": body.variable_fields,
    }
    if body.pair_with:
        fixture["pair_with"] = body.pair_with
    document = {"revenant_fixtures": "1.0", "fixtures": [fixture]}
    raw = json.dumps(document, sort_keys=True).encode("utf-8")
    return _ingest(project, session, settings, actor, "manual-example.json",
                   "application/json", raw, body.side, "manual_example")


@router.get("/evidence")
def list_evidence(project: CurrentProject, session: DbSession,
                  settings: SettingsDep) -> dict[str, Any]:
    from revenant.evidence.service import current_files

    service = evidence_service(session, settings)
    rows = current_files(session, project.id)
    return {"items": [_evidence(row, service.fact_count(row.id)) for row in rows],
            "total": len(rows), "evidence_version": project.evidence_version,
            "evidence_fingerprint": project.evidence_fingerprint}


@router.get("/evidence/missing")
def missing_evidence(project: CurrentProject, session: DbSession,
                     settings: SettingsDep) -> dict[str, Any]:
    items = [item.as_dict() for item in evidence_service(session, settings).missing(project.id)]
    return {"items": items, "total": len(items)}


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str, project: CurrentProject, session: DbSession,
                 settings: SettingsDep) -> EvidenceOut:
    row = session.get(EvidenceFile, evidence_id)
    if row is None or row.project_id != project.id or row.deleted_at is not None:
        raise NotFound("Evidence file not found.")
    return _evidence(row, evidence_service(session, settings).fact_count(row.id))


@router.delete("/evidence/{evidence_id}", status_code=204)
def delete_evidence(evidence_id: str, project: CurrentProject, session: DbSession,
                    settings: SettingsDep, actor: AdminActor) -> None:
    record = evidence_service(session, settings).remove(project, evidence_id)
    audit.record(session, actor=actor, action="evidence.remove", target_type="evidence_file",
                 target_id=record.id, project_id=project.id,
                 meta={"filename": record.filename,
                       "evidence_version": project.evidence_version})


@router.get("/endpoints")
def list_endpoints(project: CurrentProject, session: DbSession, settings: SettingsDep,
                   side: Annotated[Side | None, Query()] = None) -> dict[str, Any]:
    rows = evidence_service(session, settings).endpoints(project.id, side)
    return {"items": [_endpoint(row, detail=True) for row in rows], "total": len(rows)}


@router.get("/endpoints/{endpoint_id}/facts")
def endpoint_facts(endpoint_id: str, project: CurrentProject, session: DbSession,
                   settings: SettingsDep) -> dict[str, Any]:
    rows = evidence_service(session, settings).facts(project.id, endpoint_id)
    items = [{
        "id": fact.id, "fact_type": fact.fact_type, "subject": fact.subject,
        "value": fact.value, "basis": fact.basis, "locator": fact.locator,
        "evidence_file": {"id": source.id, "filename": source.filename, "side": source.side},
    } for fact, source in rows]
    return {"items": items, "total": len(items)}


@router.get("/fixtures")
def list_fixtures(project: CurrentProject, session: DbSession,
                  settings: SettingsDep) -> dict[str, Any]:
    rows = evidence_service(session, settings).fixtures(project.id)
    return {"items": [_fixture(row) for row in rows], "total": len(rows)}
