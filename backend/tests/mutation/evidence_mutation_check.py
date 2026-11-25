"""Mutation check for the Evidence Vault and the projects API.

Run from the ``backend`` directory:

    python tests/mutation/evidence_mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import Mutations, run  # noqa: E402

TESTS = ["tests/unit/test_evidence_parsers.py", "tests/integration/test_evidence_api.py",
         "tests/integration/test_projects_api.py"]

LOADER = "revenant/evidence/loader.py"
OPENAPI = "revenant/evidence/openapi.py"
FIXTURES = "revenant/evidence/fixtures.py"
SERVICE = "revenant/evidence/service.py"
EVIDENCE_API = "revenant/api/evidence.py"
PROJECTS_API = "revenant/api/projects.py"

MUTATIONS: Mutations = {
    # ---- untrusted input ----------------------------------------------------------
    "load YAML with the unsafe loader": (
        LOADER, "            document = yaml.safe_load(text)",
        "            document = yaml.load(text, Loader=yaml.UnsafeLoader)  # noqa: S506",
    ),
    "accept any file extension": (
        LOADER, "    if extension not in ALLOWED_EXTENSIONS:", "    if False:",
    ),
    "do not enforce the upload size limit": (
        LOADER, "    if len(raw) > max_bytes:", "    if False:",
    ),
    "do not bound document depth": (
        LOADER, "        if depth > MAX_DEPTH:", "        if False:",
    ),
    "do not bound document size": (
        LOADER, "        if seen > MAX_NODES:", "        if False:",
    ),
    "keep path components of the uploaded filename": (
        LOADER, '    base = PurePosixPath((name or "upload").replace("\\\\", "/")).name',
        '    base = name or "upload"',
    ),
    "do not detect cyclic references": (
        OPENAPI, "            if ref in trail:", "            if False:",
    ),
    "treat remote references as resolved": (
        OPENAPI,
        '                self._unresolved(ref, "remote or file reference; not fetched")\n'
        "                return None, pointer, trail",
        "                return {}, pointer, trail",
    ),
    # ---- credentials in evidence ----------------------------------------------------
    "do not redact credentials in fixtures": (
        FIXTURES, "        if is_credential_name(str(name)) and value != REDACTED:",
        "        if False:",
    ),
    "store the uploaded fixture file instead of the redacted form": (
        SERVICE,
        "        record.content = json.dumps(parsed.redacted_document, indent=2,\n"
        '                                    ensure_ascii=False).encode("utf-8")\n',
        "",
    ),
    # ---- nothing is discarded, everything has a source ------------------------------
    "silently drop files that cannot be parsed": (
        SERVICE,
        '                report["problems"] = exc.problems\n'
        '                return self._store(project, record, "failed", report)',
        '                report["problems"] = exc.problems\n'
        "                return record",
    ),
    "do not report rejected fixtures": (
        SERVICE, '        report["rejected"] = parsed.rejected\n', "",
    ),
    "do not report unresolved references": (
        SERVICE, '        report["unresolved_refs"] = spec.unresolved_refs\n', "",
    ),
    "accept invalid fixtures": (
        FIXTURES, "    if method not in _METHODS:", "    if False:",
    ),
    "associate fixtures by method only": (
        SERVICE,
        "                score = fixture_format.template_score(candidate.path_template, path)",
        "                score = 0",
    ),
    "do not pair recordings with legacy fixtures": (
        SERVICE, "            item.pair_with = target.id if target else None",
        "            item.pair_with = None",
    ),
    # ---- versioning and removal -----------------------------------------------------
    "do not bump the evidence version": (
        SERVICE, "    project.evidence_version += 1\n", "",
    ),
    "hard-delete removed evidence": (
        SERVICE, "        record.deleted_at = utcnow()\n        self.session.flush()",
        "        self.session.delete(record)\n        self.session.flush()",
    ),
    "allow importing the same file twice": (
        SERVICE, "        if existing is not None and existing.deleted_at is None:",
        "        if False:",
    ),
    "do not enforce the file limit": (
        SERVICE,
        "        if len(current_files(self.session, project.id)) >= self.max_files:",
        "        if False:",
    ),
    # ---- missing evidence ------------------------------------------------------------
    "do not report documented status codes without an example": (
        SERVICE, "            for status in sorted(documented - seen):",
        "            for status in []:",
    ),
    "do not report endpoints without fixtures": (
        SERVICE, "            if not examples:", "            if False:",
    ),
    # ---- API boundaries --------------------------------------------------------------
    "evidence routes without authentication": (
        EVIDENCE_API,
        'router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["evidence"],\n'
        "                   dependencies=[Depends(require_admin)])",
        'router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["evidence"])',
    ),
    "read evidence of another project": (
        EVIDENCE_API,
        "    if row is None or row.project_id != project.id or row.deleted_at is not None:",
        "    if row is None or row.deleted_at is not None:",
    ),
    "save the replacement URL without the outbound guard": (
        PROJECTS_API,
        "        anyio.from_thread.run(check_destination, url, settings, resolver)",
        "        pass",
    ),
    "delete a project while its runtime is running": (
        PROJECTS_API, '    if project.status == "running":', "    if False:",
    ),
    "overwrite an existing slug": (
        PROJECTS_API,
        "        if session.scalar(select(Project.id).where(Project.slug == body.slug)) "
        "is not None:",
        "        if False:",
    ),
}

if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS, TESTS))
