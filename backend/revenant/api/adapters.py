"""Adapter compilation and dry run (docs/api-contracts.md, "Adapters")."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from revenant.adapter.compiler import AdapterService
from revenant.api.deps import AdminActor, CurrentProject, DbSession, get_settings, require_admin
from revenant.config import Settings
from revenant.db.models import AdapterVersion

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["adapters"],
                   dependencies=[Depends(require_admin)])
SettingsDep = Annotated[Settings, Depends(get_settings)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompileRequest(_Body):
    notes: str = Field(default="", max_length=2000)


class DryRunRequest(_Body):
    method: Literal["GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    path: str = Field(pattern=r"^/[^?#]*$", max_length=2000)
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


def _version(row: AdapterVersion, *, document: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id, "version": row.version, "reconstruction_id": row.reconstruction_id,
        "document_sha256": row.document_sha256, "mapping_count": len(row.mapping_ids),
        "routes": row.report.get("routes", 0),
        "excluded_routes": row.report.get("excluded_routes", []),
        "warnings": row.report.get("warnings", []), "compiled_by": row.compiled_by,
        "notes": row.notes, "created_at": row.created_at,
    }
    if document:
        out["document"] = row.document
        out["mapping_ids"] = row.mapping_ids
    return out


@router.post("/adapters", status_code=201)
def compile_adapter(project: CurrentProject, session: DbSession, settings: SettingsDep,
                    actor: AdminActor, body: CompileRequest | None = None) -> dict[str, Any]:
    record = AdapterService(session, settings).compile(project, actor,
                                                       body.notes if body else "")
    return _version(record, document=True)


@router.get("/adapters")
def list_adapters(project: CurrentProject, session: DbSession,
                  settings: SettingsDep) -> dict[str, Any]:
    rows = AdapterService(session, settings).versions(project.id)
    return {"items": [_version(row) for row in rows], "total": len(rows)}


@router.get("/adapters/{version}")
def get_adapter(version: int, project: CurrentProject, session: DbSession,
                settings: SettingsDep) -> dict[str, Any]:
    return _version(AdapterService(session, settings).get(project.id, version), document=True)


@router.post("/adapters/{version}/dry-run")
def dry_run(version: int, body: DryRunRequest, project: CurrentProject, session: DbSession,
            settings: SettingsDep) -> dict[str, Any]:
    return AdapterService(session, settings).dry_run(
        project, version, body.method, body.path, dict(body.query), dict(body.headers), body.body)
