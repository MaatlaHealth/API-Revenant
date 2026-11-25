"""Project endpoints (docs/api-contracts.md, "Projects")."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Any

import anyio.from_thread
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from revenant import audit
from revenant.api.deps import AdminActor, CurrentProject, DbSession, get_settings, require_admin
from revenant.config import Settings
from revenant.db.models import AuditRecord, Project
from revenant.errors import Conflict, OutboundBlocked, UnsafeUrl
from revenant.evidence.service import EvidenceService
from revenant.security.outbound import Resolver, check_destination, system_resolver

router = APIRouter(prefix="/api/v1/projects", tags=["projects"],
                   dependencies=[Depends(require_admin)])

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
_SLUG_RE = re.compile(SLUG_PATTERN)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_Body):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    description: str = Field(default="", max_length=5000)
    legacy_name: str = Field(min_length=1, max_length=200)
    replacement_name: str = Field(min_length=1, max_length=200)
    replacement_base_url: str | None = Field(default=None, max_length=2000)


class ProjectUpdate(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    legacy_name: str | None = Field(default=None, min_length=1, max_length=200)
    replacement_name: str | None = Field(default=None, min_length=1, max_length=200)
    replacement_base_url: str | None = Field(default=None, max_length=2000)


class WorkflowStep(BaseModel):
    key: str
    title: str
    complete: bool
    available: bool
    blocked_by: str | None


class ProjectOut(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    legacy_name: str
    replacement_name: str
    replacement_base_url: str | None
    is_demo: bool
    status: str
    evidence_version: int
    evidence: dict[str, int]
    workflow: list[WorkflowStep]
    created_at: datetime
    updated_at: datetime


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:64].strip("-")
    return slug or "project"


def _unique_slug(session: Session, base: str) -> str:
    candidate, suffix = base, 1
    while session.scalar(select(Project.id).where(Project.slug == candidate)) is not None:
        suffix += 1
        tail = f"-{suffix}"
        candidate = base[: 64 - len(tail)].rstrip("-") + tail
    return candidate


def validate_base_url(url: str, settings: Settings, resolver: Resolver) -> str:
    """Apply the outbound guard when a replacement URL is configured.

    A name that does not resolve yet is accepted: the guard runs again, from a
    fresh resolution, on every connection. A name that resolves to a disallowed
    address, or a URL with an unsafe shape, is rejected now.
    """
    try:
        anyio.from_thread.run(check_destination, url, settings, resolver)
    except OutboundBlocked as exc:
        raise UnsafeUrl(
            "The replacement base URL is not allowed.",
            details=[{"field": "replacement_base_url", "issue": str(exc)}],
        ) from exc
    except OSError:
        pass  # does not resolve from here; not evidence that it is unsafe
    return url.rstrip("/")


def workflow(
    project: Project, counts: dict[str, int], missing_codes: set[str]
) -> list[WorkflowStep]:
    """Which steps are done and what blocks the next one. Computed from stored data."""
    legacy_spec = "no_specification:legacy" not in missing_codes
    replacement_spec = "no_specification:replacement" not in missing_codes
    fixtures = "no_legacy_fixtures:legacy" not in missing_codes
    evidence_ready = legacy_spec and replacement_spec and fixtures

    def blocker(*pairs: tuple[bool, str]) -> str | None:
        return next((text for ok, text in pairs if not ok), None)

    steps = [
        WorkflowStep(key="legacy_specification", title="Import the legacy API specification",
                     complete=legacy_spec, available=True, blocked_by=None),
        WorkflowStep(key="replacement_specification",
                     title="Import the replacement API specification",
                     complete=replacement_spec, available=True, blocked_by=None),
        WorkflowStep(key="fixtures", title="Import historical fixtures",
                     complete=fixtures, available=True, blocked_by=None),
    ]
    not_built = "Not available in this build yet."
    steps.append(WorkflowStep(
        key="reconstruction", title="Reconstruct and review mappings", complete=False,
        available=False,
        blocked_by=blocker((legacy_spec, "Import the legacy specification first."),
                           (replacement_spec, "Import the replacement specification first."),
                           (fixtures, "Import historical fixtures first."),
                           (not evidence_ready, not_built))))
    for key, title in (("adapter", "Compile the adapter"), ("verification", "Verify"),
                       ("runtime", "Run the compatibility runtime")):
        steps.append(WorkflowStep(key=key, title=title, complete=False, available=False,
                                  blocked_by="Requires the previous step."))
    return steps


def describe(project: Project, service: EvidenceService) -> ProjectOut:
    counts = service.counts(project.id)
    codes = {f"{item.code}:{item.side}" for item in service.missing(project.id)}
    return ProjectOut(
        id=project.id, slug=project.slug, name=project.name, description=project.description,
        legacy_name=project.legacy_name, replacement_name=project.replacement_name,
        replacement_base_url=project.replacement_base_url, is_demo=project.is_demo,
        status=project.status, evidence_version=project.evidence_version, evidence=counts,
        workflow=workflow(project, counts, codes),
        created_at=project.created_at, updated_at=project.updated_at,
    )


def evidence_service(session: Session, settings: Settings) -> EvidenceService:
    return EvidenceService(session, settings.max_upload_bytes, settings.max_evidence_files)


def _resolver(request: Request) -> Resolver:
    resolver: Resolver = getattr(request.app.state, "resolver", system_resolver)
    return resolver


SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("", status_code=201)
def create_project(body: ProjectCreate, request: Request, session: DbSession,
                   settings: SettingsDep, actor: AdminActor) -> ProjectOut:
    if body.slug is not None:
        if session.scalar(select(Project.id).where(Project.slug == body.slug)) is not None:
            raise Conflict("A project with this slug already exists.",
                           details=[{"field": "slug", "issue": "already exists"}])
        slug = body.slug
    else:
        slug = _unique_slug(session, slugify(body.name))
    base_url = None
    if body.replacement_base_url:
        base_url = validate_base_url(body.replacement_base_url, settings, _resolver(request))
    project = Project(
        slug=slug, name=body.name.strip(), description=body.description,
        legacy_name=body.legacy_name.strip(), replacement_name=body.replacement_name.strip(),
        replacement_base_url=base_url,
    )
    session.add(project)
    session.flush()
    audit.record(session, actor=actor, action="project.create", target_type="project",
                 target_id=project.id, project_id=project.id, meta={"slug": slug})
    return describe(project, evidence_service(session, settings))


@router.get("")
def list_projects(session: DbSession, settings: SettingsDep,
                  limit: Annotated[int, Query(ge=1, le=200)] = 50,
                  offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(Project)) or 0
    rows = session.scalars(
        select(Project).order_by(Project.created_at.desc(), Project.slug)
        .limit(limit).offset(offset))
    service = evidence_service(session, settings)
    return {"items": [describe(row, service) for row in rows], "total": total}


@router.get("/{project_id}")
def get_project(project: CurrentProject, session: DbSession,
                settings: SettingsDep) -> ProjectOut:
    return describe(project, evidence_service(session, settings))


@router.patch("/{project_id}")
def update_project(body: ProjectUpdate, request: Request, project: CurrentProject,
                   session: DbSession, settings: SettingsDep, actor: AdminActor) -> ProjectOut:
    changed: list[str] = []
    supplied = body.model_dump(exclude_unset=True)
    for name in ("name", "description", "legacy_name", "replacement_name"):
        if name in supplied and supplied[name] is not None:
            setattr(project, name, supplied[name].strip() if name != "description"
                    else supplied[name])
            changed.append(name)
    if "replacement_base_url" in supplied:
        url = supplied["replacement_base_url"]
        project.replacement_base_url = (
            validate_base_url(url, settings, _resolver(request)) if url else None)
        changed.append("replacement_base_url")
    session.flush()
    if changed:
        audit.record(session, actor=actor, action="project.update", target_type="project",
                     target_id=project.id, project_id=project.id, meta={"fields": changed})
    return describe(project, evidence_service(session, settings))


@router.delete("/{project_id}", status_code=204)
def delete_project(project: CurrentProject, session: DbSession, actor: AdminActor) -> None:
    if project.status == "running":
        raise Conflict("The project's runtime is running. Stop it before deleting the project.",
                       details=[{"field": "status", "issue": "runtime is running"}])
    audit.record(session, actor=actor, action="project.delete", target_type="project",
                 target_id=project.id, project_id=None,
                 meta={"slug": project.slug, "name": project.name})
    session.delete(project)


@router.get("/{project_id}/activity")
def project_activity(project: CurrentProject, session: DbSession,
                     limit: Annotated[int, Query(ge=1, le=200)] = 50,
                     offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    base = select(AuditRecord).where(AuditRecord.project_id == project.id)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(
        base.order_by(AuditRecord.created_at.desc(), AuditRecord.id).limit(limit).offset(offset))
    items = [{"id": row.id, "actor": row.actor, "action": row.action,
              "target_type": row.target_type, "target_id": row.target_id,
              "metadata": row.meta, "created_at": row.created_at} for row in rows]
    return {"items": items, "total": total}
