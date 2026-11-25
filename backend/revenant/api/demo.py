"""The demonstration project (docs/api-contracts.md, ``POST /demo/seed``).

GravePay and NovaPay are fictional. The project is flagged ``is_demo`` so the
interface can label everything in it as demonstration data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from revenant import audit
from revenant.api.deps import AdminActor, DbSession, get_settings, require_admin
from revenant.api.projects import ProjectOut, describe, evidence_service
from revenant.config import Settings
from revenant.db.models import Project
from revenant.errors import Conflict, NotConfigured
from revenant.runtime.service import after_commit

router = APIRouter(prefix="/api/v1/demo", tags=["demo"], dependencies=[Depends(require_admin)])

SLUG = "gravepay-demo"
FILES = (
    ("gravepay/openapi.yaml", "legacy"), ("gravepay/fixtures.json", "legacy"),
    ("gravepay/notes.md", "legacy"), ("novapay/openapi.yaml", "replacement"),
    ("novapay/recordings.json", "replacement"),
)


def demo_directory(settings: Settings) -> Path:
    if settings.demo_dir:
        return Path(settings.demo_dir)
    return Path(__file__).resolve().parents[3] / "demo"


@router.post("/seed")
def seed_demo(request: Request, session: DbSession, actor: AdminActor,
              settings: Annotated[Settings, Depends(get_settings)]) -> ProjectOut:
    """Create the demonstration project and import its evidence. Calling it again
    changes nothing."""
    root = demo_directory(settings)
    if not all((root / name).is_file() for name, _ in FILES):
        raise NotConfigured("The demonstration files are not available on this server.")
    project = session.scalar(select(Project).where(Project.slug == SLUG))
    if project is not None and not project.is_demo:
        raise Conflict("A project that is not the demonstration already uses its slug.",
                       details=[{"field": "slug", "issue": "already exists"}])
    created = project is None
    if project is None:
        project = Project(
            slug=SLUG, name="GravePay to NovaPay (demonstration)",
            description="GravePay and NovaPay are fictional services created for this "
                        "demonstration. All records are sample data.",
            legacy_name="GravePay (fictional)", replacement_name="NovaPay (fictional)",
            is_demo=True)
        session.add(project)
        session.flush()
    service = evidence_service(session, settings)
    imported = 0
    for name, side in FILES:
        path = root / name
        try:
            service.ingest(project, path.name, "", path.read_bytes(), side)
            imported += 1
        except Conflict:
            continue  # already imported: the seed is idempotent
    if created or imported:
        audit.record(session, actor=actor, action="demo.seed", target_type="project",
                     target_id=project.id, project_id=project.id,
                     meta={"created": created, "files_imported": imported})
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        project_id = project.id
        after_commit(session, lambda: runtime.slugs.__setitem__(SLUG, project_id))
    return describe(project, service)
