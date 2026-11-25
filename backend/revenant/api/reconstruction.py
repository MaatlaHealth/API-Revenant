"""Reconstruction, mapping review, operation effects, gaps, invariants and the
behaviour inventory (docs/api-contracts.md, "Reconstruction and mappings")."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from revenant.api.deps import AdminActor, CurrentProject, DbSession, require_admin
from revenant.db.models import (
    Behaviour,
    Gap,
    Invariant,
    Mapping,
    Project,
    Reconstruction,
)
from revenant.reconstruction.service import ReconstructionService

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["reconstruction"],
                   dependencies=[Depends(require_admin)])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
Category = Literal["endpoint", "request_field", "response_field", "query", "header", "auth",
                   "pagination", "status_code", "error", "enum", "type", "effect"]
Names = dict[str, tuple[str, bool]]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Note(_Body):
    note: str = Field(default="", max_length=2000)


class MappingEdit(_Body):
    transformation: dict[str, Any]
    note: str = Field(default="", max_length=2000)


class ObservedFilter(_Body):
    # There is no way to select inferred mappings by filter.
    confidence: Literal["observed"]
    category: Category | None = None


class BulkApprove(_Body):
    ids: list[str] | None = Field(default=None, max_length=500)
    filter: ObservedFilter | None = None
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _one(self) -> BulkApprove:
        if (self.ids is None) == (self.filter is None):
            raise ValueError("give exactly one of ids and filter")
        return self


class EffectDeclaration(_Body):
    effect: Literal["read_only", "state_changing", "unknown"]
    note: str = Field(min_length=1, max_length=2000)


class GapResolution(_Body):
    resolution: Literal["open", "accepted_risk"]
    note: str = Field(default="", max_length=2000)


class ScopeChange(_Body):
    scope: Literal["in_scope", "out_of_scope"]
    note: str = Field(default="", max_length=2000)


# ---- serialisation ------------------------------------------------------------------------


def _refs(refs: list[Any], names: Names) -> list[dict[str, Any]]:
    """Evidence references with the file they point into, including removed files."""
    out: list[dict[str, Any]] = []
    for ref in refs:
        filename, removed = names.get(ref.get("evidence_file_id", ""), (None, False))
        out.append({**ref, "filename": ref.get("filename") or filename, "file_removed": removed})
    return out


def _reconstruction(service: ReconstructionService, project: Project,
                    row: Reconstruction, *, detail: bool = False) -> dict[str, Any]:
    stale = service.is_stale(project, row)
    out: dict[str, Any] = {
        "id": row.id, "sequence": row.sequence, "status": row.status,
        "engine_version": row.engine_version, "created_at": row.created_at,
        "created_by": row.created_by, "evidence_version": row.evidence_version,
        "evidence_fingerprint": row.evidence_fingerprint, "stale": stale,
        "evidence_files_not_analysed": service.unanalysed_files(project, row) if stale else 0,
        "inventory_size": row.inventory_size, "inventory_fingerprint": row.inventory_fingerprint,
        "inventory_diff": row.inventory_diff, "summary": row.summary,
        "previous_reconstruction_id": row.previous_reconstruction_id,
    }
    if detail:
        report = row.report
        out["endpoint_pairs"] = [
            {"legacy": f"{pair['legacy']['method']} {pair['legacy']['path']}",
             "replacement": f"{pair['replacement']['method']} {pair['replacement']['path']}",
             "confidence": pair["confidence"], "score": pair["score"],
             "paired_recordings": pair["paired_recordings"]} for pair in report["pairs"]]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pair in report["pairs"]:
            for item in pair["differences"]:
                grouped.setdefault(item["category"], []).append(
                    {"endpoint": f"{pair['legacy']['method']} {pair['legacy']['path']}", **item})
        for item in report.get("project", []):
            grouped.setdefault(item["category"], []).append({"endpoint": None, **item})
        out["differences"] = dict(sorted(grouped.items()))
        out["unpaired_legacy"] = [
            {"endpoint": f"{item['legacy']['method']} {item['legacy']['path']}",
             "reason": item["reason"]} for item in report["unpaired_legacy"]]
        out["unpaired_replacement"] = [f"{item['method']} {item['path']}"
                                       for item in report["unpaired_replacement"]]
    return out


def _mapping(row: Mapping, names: Names, service: ReconstructionService | None = None
             ) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id, "reconstruction_id": row.reconstruction_id, "key": row.key,
        "category": row.category, "legacy_endpoint_id": row.legacy_endpoint_id,
        "replacement_endpoint_id": row.replacement_endpoint_id, "legacy_ref": row.legacy_ref,
        "replacement_ref": row.replacement_ref, "transformation": row.transformation,
        "confidence": row.confidence, "rule_name": row.rule_name, "rationale": row.rationale,
        "evidence_refs": _refs(row.evidence_refs, names),
        "requires_approval": row.requires_approval, "status": row.status, "origin": row.origin,
        "supersedes_id": row.supersedes_id, "created_at": row.created_at,
    }
    if service is not None:
        out["decisions"] = [{"decision": item.decision, "actor": item.actor, "note": item.note,
                             "created_at": item.created_at}
                            for item in service.decisions(row.id)]
    return out


def _gap(row: Gap, names: Names) -> dict[str, Any]:
    return {"id": row.id, "reconstruction_id": row.reconstruction_id, "key": row.key,
            "endpoint_id": row.endpoint_id, "endpoint": row.endpoint_ref,
            "category": row.category, "title": row.title, "description": row.description,
            "evidence_refs": _refs(row.evidence_refs, names), "missing": row.missing,
            "severity": row.severity, "behaviour_key": row.behaviour_key,
            "resolution": row.resolution, "resolution_note": row.resolution_note}


def _invariant(row: Invariant, names: Names) -> dict[str, Any]:
    return {"id": row.id, "reconstruction_id": row.reconstruction_id, "key": row.key,
            "endpoint_id": row.endpoint_id, "endpoint": row.endpoint_ref, "kind": row.kind,
            "definition": row.definition, "confidence": row.confidence,
            "evidence_refs": _refs(row.evidence_refs, names), "status": row.status}


def _behaviour(row: Behaviour, names: Names) -> dict[str, Any]:
    return {"id": row.id, "reconstruction_id": row.reconstruction_id, "key": row.key,
            "kind": row.kind, "endpoint_id": row.endpoint_id, "endpoint": row.endpoint_ref,
            "group": "endpoint" if row.endpoint_ref else "project",
            "description": row.description, "evidence_refs": _refs(row.evidence_refs, names),
            "has_evidence": bool(row.evidence_refs) and row.kind != "unsupported",
            "scope": row.scope, "scope_note": row.scope_note,
            "unsupported_reason": row.unsupported_reason,
            # No verification run exists yet, so nothing has been assessed.
            "state": "not_assessed", "state_reason": "no verification run on this inventory"}


def _page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    return {"items": items[offset:offset + limit], "total": len(items)}


# ---- reconstructions ----------------------------------------------------------------------


@router.post("/reconstructions", status_code=201)
def run_reconstruction(project: CurrentProject, session: DbSession,
                       actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    return _reconstruction(service, project, service.run(project, actor), detail=True)


@router.get("/reconstructions")
def list_reconstructions(project: CurrentProject, session: DbSession, limit: Limit = 50,
                         offset: Offset = 0) -> dict[str, Any]:
    service = ReconstructionService(session)
    return _page([_reconstruction(service, project, row) for row in service.history(project.id)],
                 limit, offset)


@router.get("/reconstructions/latest")
def latest_reconstruction(project: CurrentProject, session: DbSession) -> dict[str, Any]:
    service = ReconstructionService(session)
    return _reconstruction(service, project, service.require_latest(project.id), detail=True)


@router.get("/reconstructions/{reconstruction_id}/behaviours")
def past_inventory(reconstruction_id: str, project: CurrentProject, session: DbSession,
                   limit: Limit = 200, offset: Offset = 0) -> dict[str, Any]:
    service = ReconstructionService(session)
    record = service.get(project.id, reconstruction_id)
    names = service.filenames(project.id)
    page = _page([_behaviour(row, names) for row in service.behaviours(record.id)], limit, offset)
    return {**page, "reconstruction_id": record.id, "inventory_size": record.inventory_size,
            "inventory_fingerprint": record.inventory_fingerprint}


@router.get("/comparison")
def comparison(project: CurrentProject, session: DbSession) -> dict[str, Any]:
    """The two contracts side by side, as the latest reconstruction analysed them."""
    service = ReconstructionService(session)
    record = service.require_latest(project.id)
    return {"reconstruction_id": record.id, "stale": service.is_stale(project, record),
            "pairs": record.report["pairs"], "unpaired_legacy": record.report["unpaired_legacy"],
            "unpaired_replacement": record.report["unpaired_replacement"],
            "project": record.report.get("project", [])}


# ---- mappings -----------------------------------------------------------------------------


@router.get("/mappings")
def list_mappings(
    project: CurrentProject, session: DbSession,
    status: Annotated[Literal["proposed", "approved", "rejected", "superseded"] | None,
                      Query()] = None,
    category: Annotated[Category | None, Query()] = None,
    confidence: Annotated[Literal["observed", "inferred"] | None, Query()] = None,
    limit: Limit = 50, offset: Offset = 0,
) -> dict[str, Any]:
    service = ReconstructionService(session)
    record = service.require_latest(project.id)
    names = service.filenames(project.id)
    rows = service.mappings(record.id, status=status, category=category, confidence=confidence)
    return {**_page([_mapping(row, names) for row in rows], limit, offset),
            "reconstruction_id": record.id, "stale": service.is_stale(project, record)}


@router.post("/mappings/bulk-approve")
def bulk_approve(body: BulkApprove, project: CurrentProject, session: DbSession,
                 actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    result = service.bulk_approve(
        project, actor, ids=body.ids, note=body.note,
        category=body.filter.category if body.filter else None)
    return {"approved": [row.id for row in result["approved"]],
            "approved_count": len(result["approved"]), "skipped": result["skipped"]}


@router.get("/mappings/{mapping_id}")
def get_mapping(mapping_id: str, project: CurrentProject, session: DbSession) -> dict[str, Any]:
    service = ReconstructionService(session)
    return _mapping(service.mapping(project.id, mapping_id), service.filenames(project.id),
                    service)


@router.post("/mappings/{mapping_id}/approve")
def approve_mapping(mapping_id: str, project: CurrentProject, session: DbSession,
                    actor: AdminActor, body: Note | None = None) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.decide(project, service.mapping(project.id, mapping_id), "approved", actor,
                         body.note if body else "")
    return _mapping(row, service.filenames(project.id), service)


@router.post("/mappings/{mapping_id}/reject")
def reject_mapping(mapping_id: str, project: CurrentProject, session: DbSession,
                   actor: AdminActor, body: Note | None = None) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.decide(project, service.mapping(project.id, mapping_id), "rejected", actor,
                         body.note if body else "")
    return _mapping(row, service.filenames(project.id), service)


@router.put("/mappings/{mapping_id}")
def edit_mapping(mapping_id: str, body: MappingEdit, project: CurrentProject,
                 session: DbSession, actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.edit(project, service.mapping(project.id, mapping_id), body.transformation,
                       actor, body.note)
    return _mapping(row, service.filenames(project.id), service)


# ---- operations and their effect ------------------------------------------------------------


@router.get("/operations")
def list_operations(project: CurrentProject, session: DbSession) -> dict[str, Any]:
    items = ReconstructionService(session).operations(project)
    return {"items": items, "total": len(items)}


@router.put("/operations/{operation_id}/effect")
def declare_effect(operation_id: str, body: EffectDeclaration, project: CurrentProject,
                   session: DbSession, actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    service.declare_effect(project, operation_id, body.effect, body.note, actor)
    return next(item for item in service.operations(project) if item["id"] == operation_id)


# ---- gaps, invariants, behaviours ------------------------------------------------------------


@router.get("/gaps")
def list_gaps(project: CurrentProject, session: DbSession, limit: Limit = 50,
              offset: Offset = 0) -> dict[str, Any]:
    service = ReconstructionService(session)
    record = service.require_latest(project.id)
    names = service.filenames(project.id)
    return {**_page([_gap(row, names) for row in service.gaps(record.id)], limit, offset),
            "reconstruction_id": record.id}


@router.patch("/gaps/{gap_id}")
def resolve_gap(gap_id: str, body: GapResolution, project: CurrentProject, session: DbSession,
                actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.resolve_gap(project, gap_id, body.resolution, body.note, actor)
    return _gap(row, service.filenames(project.id))


@router.get("/invariants")
def list_invariants(project: CurrentProject, session: DbSession, limit: Limit = 50,
                    offset: Offset = 0) -> dict[str, Any]:
    service = ReconstructionService(session)
    record = service.require_latest(project.id)
    names = service.filenames(project.id)
    return {**_page([_invariant(row, names) for row in service.invariants(record.id)],
                    limit, offset), "reconstruction_id": record.id}


@router.post("/invariants/{invariant_id}/approve")
def approve_invariant(invariant_id: str, project: CurrentProject, session: DbSession,
                      actor: AdminActor, body: Note | None = None) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.decide_invariant(project, invariant_id, True, actor, body.note if body else "")
    return _invariant(row, service.filenames(project.id))


@router.post("/invariants/{invariant_id}/reject")
def reject_invariant(invariant_id: str, project: CurrentProject, session: DbSession,
                     actor: AdminActor, body: Note | None = None) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.decide_invariant(project, invariant_id, False, actor, body.note if body else "")
    return _invariant(row, service.filenames(project.id))


@router.get("/behaviours")
def list_behaviours(project: CurrentProject, session: DbSession, limit: Limit = 200,
                    offset: Offset = 0) -> dict[str, Any]:
    service = ReconstructionService(session)
    record = service.require_latest(project.id)
    names = service.filenames(project.id)
    page = _page([_behaviour(row, names) for row in service.behaviours(record.id)], limit, offset)
    return {**page, "reconstruction_id": record.id, "inventory_size": record.inventory_size,
            "inventory_fingerprint": record.inventory_fingerprint,
            "stale": service.is_stale(project, record)}


@router.patch("/behaviours/{behaviour_id}")
def set_behaviour_scope(behaviour_id: str, body: ScopeChange, project: CurrentProject,
                        session: DbSession, actor: AdminActor) -> dict[str, Any]:
    service = ReconstructionService(session)
    row = service.set_scope(project, behaviour_id, body.scope, body.note, actor)
    return _behaviour(row, service.filenames(project.id))
