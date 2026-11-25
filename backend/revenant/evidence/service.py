"""Evidence ingestion: store the file, extract facts with provenance, and keep
the project's evidence version current.

Rules enforced here:
* nothing is discarded silently — a file that cannot be parsed is stored with a
  report listing every problem;
* every fact row names the evidence file and the location that supports it;
* removing a file is a soft delete, so historical reconstructions and runs can
  still be explained.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from revenant.db.base import utcnow
from revenant.db.models import ApiSpec, Endpoint, EndpointFact, EvidenceFile, Fixture, Project
from revenant.errors import Conflict, NotFound, ValidationFailed
from revenant.evidence import fixtures as fixture_format
from revenant.evidence.loader import (
    UnparseableDocument,
    check_upload,
    detect_kind,
    extension_of,
    load_structured,
    safe_filename,
)
from revenant.evidence.openapi import ParsedSpec, parse_spec

SPEC_KINDS = ("openapi", "swagger")
STORED_ONLY_KINDS = {
    "markdown": "Stored as documentation. Free text is not analysed in this release.",
    "json_schema": "Stored. Standalone JSON Schema files are not analysed in this release; "
                   "import an OpenAPI document or fixtures to extract facts.",
}


@dataclass
class MissingEvidence:
    code: str
    message: str
    side: str | None = None
    endpoint_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "side": self.side,
                "endpoint_id": self.endpoint_id}


def current_files(session: Session, project_id: str) -> list[EvidenceFile]:
    return list(session.scalars(
        select(EvidenceFile)
        .where(EvidenceFile.project_id == project_id, EvidenceFile.deleted_at.is_(None))
        .order_by(EvidenceFile.created_at, EvidenceFile.filename)
    ))


def refresh_evidence_version(session: Session, project: Project) -> None:
    """Bump the version and recompute the fingerprint of the current evidence set."""
    hashes = sorted(item.sha256 for item in current_files(session, project.id))
    project.evidence_fingerprint = hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()
    project.evidence_version += 1
    if project.status == "draft" and hashes:
        project.status = "evidence"
    session.flush()


class EvidenceService:
    def __init__(self, session: Session, max_upload_bytes: int, max_files: int) -> None:
        self.session = session
        self.max_upload_bytes = max_upload_bytes
        self.max_files = max_files

    # ------------------------------------------------------------------ ingest

    def ingest(self, project: Project, filename: str | None, content_type: str | None,
               raw: bytes, side: str, declared_kind: str | None = None) -> EvidenceFile:
        name = safe_filename(filename)
        text = check_upload(name, raw, self.max_upload_bytes)  # raises EvidenceRejected
        digest = hashlib.sha256(raw).hexdigest()

        existing = self.session.scalar(select(EvidenceFile).where(
            EvidenceFile.project_id == project.id, EvidenceFile.sha256 == digest))
        if existing is not None and existing.deleted_at is None:
            raise Conflict(
                "This file has already been imported into the project.",
                details=[{"field": "file", "issue": f"identical to {existing.filename}"}],
            )
        if len(current_files(self.session, project.id)) >= self.max_files:
            raise ValidationFailed(
                f"A project can hold at most {self.max_files} evidence files.",
                details=[{"field": "file", "issue": "file limit reached"}],
            )
        if existing is not None:
            # Re-importing a removed file: discard the old rows and start again.
            self.session.delete(existing)
            self.session.flush()

        record = EvidenceFile(
            project_id=project.id, side=side, kind="markdown", filename=name,
            content_type=(content_type or "")[:100], size_bytes=len(raw), sha256=digest,
            content=raw, parse_status="failed", parse_report={},
        )
        report: dict[str, Any] = {"problems": [], "warnings": []}

        if extension_of(name) in (".md", ".txt"):
            kind = "markdown"
            document: Any = None
        else:
            try:
                document = load_structured(text, name)
            except UnparseableDocument as exc:
                record.kind = declared_kind or "unknown"
                report["problems"] = exc.problems
                return self._store(project, record, "failed", report)
            kind = detect_kind(document, name) or ""
            if not kind:
                record.kind = declared_kind or "unknown"
                report["problems"] = [
                    "not recognised as an OpenAPI 3.x document, a Swagger 2.0 document, "
                    "a fixture file or a JSON Schema"
                ]
                return self._store(project, record, "failed", report)
        if declared_kind and declared_kind != kind and not (
            declared_kind == "manual_example" and kind == "fixtures"
        ):
            record.kind = kind
            report["problems"] = [f"declared as {declared_kind} but the content is {kind}"]
            return self._store(project, record, "failed", report)
        record.kind = "manual_example" if declared_kind == "manual_example" else kind

        if kind in STORED_ONLY_KINDS:
            report["warnings"] = [STORED_ONLY_KINDS[kind]]
            return self._store(project, record, "unsupported", report)
        if kind in SPEC_KINDS:
            return self._ingest_spec(project, record, document, report)
        return self._ingest_fixtures(project, record, document, report)

    def _store(self, project: Project, record: EvidenceFile, status: str,
               report: dict[str, Any]) -> EvidenceFile:
        record.parse_status = status
        record.parse_report = report
        self.session.add(record)
        self.session.flush()
        refresh_evidence_version(self.session, project)
        return record

    def _ingest_spec(self, project: Project, record: EvidenceFile, document: dict[str, Any],
                     report: dict[str, Any]) -> EvidenceFile:
        spec: ParsedSpec = parse_spec(document)
        report["warnings"] = spec.warnings
        report["unresolved_refs"] = spec.unresolved_refs
        if not spec.endpoints:
            report["problems"] = ["no operations could be read from the specification"]
            return self._store(project, record, "failed", report)
        self.session.add(record)
        self.session.flush()

        api_spec = ApiSpec(
            evidence_file_id=record.id, project_id=project.id, side=record.side,
            title=spec.title, version=spec.version, spec_format=spec.spec_format,
            servers=spec.servers,
            auth_schemes={name: item["value"] for name, item in spec.auth_schemes.items()},
        )
        self.session.add(api_spec)
        self.session.flush()

        fact_count = 0
        for parsed in spec.endpoints:
            endpoint = self._endpoint(project.id, record.side, parsed.method, parsed.path)
            endpoint.api_spec_id = api_spec.id
            endpoint.operation_id = parsed.operation_id
            endpoint.summary = parsed.summary
            endpoint.parameters = parsed.parameters
            endpoint.request_schema = parsed.request_schema
            endpoint.response_schemas = parsed.response_schemas
            self.session.flush()
            for fact in parsed.facts:
                self.session.add(EndpointFact(
                    endpoint_id=endpoint.id, fact_type=fact.fact_type,
                    subject=fact.subject[:500], value=fact.value,
                    evidence_file_id=record.id, locator=fact.locator[:1000], basis="declared",
                ))
                fact_count += 1
        report["endpoints"] = len(spec.endpoints)
        report["facts"] = fact_count
        status = "partial" if (spec.unresolved_refs or spec.warnings) else "parsed"
        record.parse_status, record.parse_report = status, report
        self.session.flush()
        self.reassociate(project.id)
        refresh_evidence_version(self.session, project)
        return record

    def _ingest_fixtures(self, project: Project, record: EvidenceFile, document: Any,
                         report: dict[str, Any]) -> EvidenceFile:
        try:
            parsed = fixture_format.parse_fixture_file(document)
        except ValueError as exc:
            report["problems"] = [str(exc)]
            return self._store(project, record, "failed", report)
        report["rejected"] = parsed.rejected
        report["redacted_credentials"] = parsed.redactions
        if not parsed.fixtures:
            report["problems"] = ["the file contains no valid fixtures"]
            # Store the redacted form even on failure: the upload may hold credentials.
            record.content = json.dumps(parsed.redacted_document, indent=2).encode("utf-8")
            return self._store(project, record, "failed", report)

        # Keep the redacted document, not the upload: credentials are not retained.
        record.content = json.dumps(parsed.redacted_document, indent=2,
                                    ensure_ascii=False).encode("utf-8")
        self.session.add(record)
        self.session.flush()
        for item in parsed.fixtures:
            self.session.add(Fixture(
                project_id=project.id, evidence_file_id=record.id, side=record.side,
                name=item.name, pair_with_name=item.pair_with, request=item.request,
                expected_response=item.response, content_sha256=item.content_sha256,
                recorded_at=item.recorded_at,
                normalisation={"variable_fields": item.variable_fields},
            ))
        self.session.flush()
        report["fixtures"] = len(parsed.fixtures)
        unpaired = self.reassociate(project.id)
        mine = [name for file_id, name in unpaired if file_id == record.id]
        if mine:
            report["warnings"].append(
                "pair_with names no legacy fixture in this project: " + ", ".join(sorted(mine)))
        record.parse_status = "partial" if (parsed.rejected or mine) else "parsed"
        record.parse_report = report
        self.session.flush()
        refresh_evidence_version(self.session, project)
        return record

    def _endpoint(self, project_id: str, side: str, method: str, path: str) -> Endpoint:
        endpoint = self.session.scalar(select(Endpoint).where(
            Endpoint.project_id == project_id, Endpoint.side == side,
            Endpoint.method == method, Endpoint.path_template == path[:500]))
        if endpoint is None:
            endpoint = Endpoint(project_id=project_id, side=side, method=method,
                                path_template=path[:500])
            self.session.add(endpoint)
            self.session.flush()
        return endpoint

    # ------------------------------------------------------------------ association

    def reassociate(self, project_id: str) -> list[tuple[str, str]]:
        """Link fixtures to endpoints and paired recordings to legacy fixtures.

        Run after every import or removal, because a specification may arrive
        after the fixtures that belong to it. Returns (evidence file id, fixture
        name) for recordings whose ``pair_with`` names no current legacy fixture.
        """
        session = self.session
        current_ids = {item.id for item in current_files(session, project_id)}
        fixtures = [item for item in session.scalars(
            select(Fixture).where(Fixture.project_id == project_id).order_by(Fixture.name))
            if item.evidence_file_id in current_ids]
        endpoints = list(session.scalars(
            select(Endpoint).where(Endpoint.project_id == project_id)))
        declared = [item for item in endpoints if item.api_spec_id is not None]

        # Facts that fixtures contributed are rebuilt from scratch each time.
        fixture_fact_ids = [fact.id for fact in session.scalars(
            select(EndpointFact).where(EndpointFact.basis == "exemplified",
                                       EndpointFact.endpoint_id.in_([e.id for e in endpoints])))
        ] if endpoints else []
        for fact_id in fixture_fact_ids:
            session.delete(session.get(EndpointFact, fact_id))
        session.flush()

        for item in fixtures:
            method, path = item.request["method"], item.request["path"]
            best: tuple[int, Endpoint] | None = None
            for candidate in declared:
                if candidate.side != item.side or candidate.method != method:
                    continue
                score = fixture_format.template_score(candidate.path_template, path)
                if score is not None and (best is None or score > best[0]):
                    best = (score, candidate)
            endpoint = best[1] if best else self._endpoint(project_id, item.side, method, path)
            item.endpoint_id = endpoint.id
            status = str(item.expected_response["status"])
            session.add(EndpointFact(
                endpoint_id=endpoint.id, fact_type="status_code", subject=status,
                value={"example": item.name}, evidence_file_id=item.evidence_file_id,
                locator=f"fixture:{item.name}", basis="exemplified",
            ))
        session.flush()

        # Endpoints that existed only because of a fixture that now has a better home.
        used = {item.endpoint_id for item in fixtures}
        declared_fact_owners = set(session.scalars(
            select(EndpointFact.endpoint_id).where(EndpointFact.basis == "declared").distinct()))
        for endpoint in endpoints:
            # Never delete an endpoint a specification described, even a removed one:
            # its facts are part of the history of the evidence.
            if (endpoint.api_spec_id is None and endpoint.id not in used
                    and endpoint.id not in declared_fact_owners):
                session.delete(endpoint)
        session.flush()

        legacy_by_name = {item.name: item for item in fixtures if item.side == "legacy"}
        unpaired: list[tuple[str, str]] = []
        for item in fixtures:
            if item.side != "replacement" or not item.pair_with_name:
                continue
            target = legacy_by_name.get(item.pair_with_name)
            item.pair_with = target.id if target else None
            if target is None:
                unpaired.append((item.evidence_file_id, item.name))
        session.flush()
        return unpaired

    # ------------------------------------------------------------------ removal

    def remove(self, project: Project, evidence_id: str) -> EvidenceFile:
        record = self.session.get(EvidenceFile, evidence_id)
        if record is None or record.project_id != project.id or record.deleted_at is not None:
            raise NotFound("Evidence file not found.")
        record.deleted_at = utcnow()
        self.session.flush()
        # A specification that is no longer current no longer declares its endpoints.
        current_ids = {item.id for item in current_files(self.session, project.id)}
        specs = {spec.id: spec for spec in self.session.scalars(
            select(ApiSpec).where(ApiSpec.project_id == project.id))}
        for endpoint in self.session.scalars(
                select(Endpoint).where(Endpoint.project_id == project.id)):
            spec = specs.get(endpoint.api_spec_id or "")
            if spec is not None and spec.evidence_file_id not in current_ids:
                replacement = self._other_declaring_spec(endpoint, specs, current_ids)
                endpoint.api_spec_id = replacement
        self.session.flush()
        self.reassociate(project.id)
        refresh_evidence_version(self.session, project)
        return record

    def _other_declaring_spec(self, endpoint: Endpoint, specs: dict[str, ApiSpec],
                              current_ids: set[str]) -> str | None:
        file_ids = set(self.session.scalars(
            select(EndpointFact.evidence_file_id).where(
                EndpointFact.endpoint_id == endpoint.id, EndpointFact.basis == "declared")))
        for spec in specs.values():
            if spec.evidence_file_id in file_ids and spec.evidence_file_id in current_ids:
                return spec.id
        return None

    # ------------------------------------------------------------------ queries

    def endpoints(self, project_id: str, side: str | None = None) -> list[Endpoint]:
        """Endpoints supported by at least one fact from a current evidence file."""
        current_ids = {item.id for item in current_files(self.session, project_id)}
        if not current_ids:
            return []
        supported = set(self.session.scalars(
            select(EndpointFact.endpoint_id).where(
                EndpointFact.evidence_file_id.in_(current_ids)).distinct()))
        query = select(Endpoint).where(Endpoint.project_id == project_id)
        if side:
            query = query.where(Endpoint.side == side)
        return [item for item in self.session.scalars(
            query.order_by(Endpoint.side, Endpoint.path_template, Endpoint.method))
            if item.id in supported]

    def facts(self, project_id: str, endpoint_id: str) -> list[tuple[EndpointFact, EvidenceFile]]:
        endpoint = self.session.get(Endpoint, endpoint_id)
        if endpoint is None or endpoint.project_id != project_id:
            raise NotFound("Endpoint not found.")
        rows = self.session.execute(
            select(EndpointFact, EvidenceFile)
            .join(EvidenceFile, EvidenceFile.id == EndpointFact.evidence_file_id)
            .where(EndpointFact.endpoint_id == endpoint_id, EvidenceFile.deleted_at.is_(None))
            .order_by(EndpointFact.fact_type, EndpointFact.subject, EndpointFact.basis)
        ).all()
        return [(fact, source) for fact, source in rows]

    def fixtures(self, project_id: str) -> list[Fixture]:
        current_ids = {item.id for item in current_files(self.session, project_id)}
        return [item for item in self.session.scalars(
            select(Fixture).where(Fixture.project_id == project_id)
            .order_by(Fixture.side, Fixture.name))
            if item.evidence_file_id in current_ids]

    def counts(self, project_id: str) -> dict[str, int]:
        files = current_files(self.session, project_id)
        return {
            "files": len(files),
            "legacy_files": sum(1 for item in files if item.side == "legacy"),
            "replacement_files": sum(1 for item in files if item.side == "replacement"),
            "failed_files": sum(1 for item in files if item.parse_status == "failed"),
            "endpoints": len(self.endpoints(project_id)),
            "fixtures": len(self.fixtures(project_id)),
        }

    def fact_count(self, evidence_id: str) -> int:
        return int(self.session.scalar(
            select(func.count()).select_from(EndpointFact)
            .where(EndpointFact.evidence_file_id == evidence_id)) or 0)

    # ------------------------------------------------------------------ missing evidence

    def missing(self, project_id: str) -> list[MissingEvidence]:
        """What the current evidence does not establish. Computed, never stored."""
        files = current_files(self.session, project_id)
        endpoints = self.endpoints(project_id)
        fixtures = self.fixtures(project_id)
        found: list[MissingEvidence] = []

        for side, label in (("legacy", "legacy"), ("replacement", "replacement")):
            has_spec = any(item.side == side and item.kind in SPEC_KINDS
                           and item.parse_status in ("parsed", "partial") for item in files)
            if not has_spec:
                found.append(MissingEvidence(
                    "no_specification", f"No {label} API specification has been imported.", side))
        if not any(item.side == "legacy" for item in fixtures):
            found.append(MissingEvidence(
                "no_legacy_fixtures", "No historical legacy fixtures have been imported.",
                "legacy"))

        for item in files:
            if item.parse_status == "failed":
                found.append(MissingEvidence(
                    "file_not_parsed", f"{item.filename} could not be parsed.", item.side))
            for ref in item.parse_report.get("unresolved_refs", []):
                found.append(MissingEvidence(
                    "unresolved_reference", f"{item.filename}: unresolved reference {ref}.",
                    item.side))
            if item.parse_report.get("rejected"):
                count = len(item.parse_report["rejected"])
                found.append(MissingEvidence(
                    "fixtures_rejected", f"{item.filename}: {count} fixture(s) were rejected.",
                    item.side))

        by_endpoint: dict[str, list[Fixture]] = {}
        for fixture in fixtures:
            by_endpoint.setdefault(fixture.endpoint_id or "", []).append(fixture)
        current_ids = {item.id for item in files}
        for endpoint in endpoints:
            if endpoint.side != "legacy":
                continue
            name = f"{endpoint.method} {endpoint.path_template}"
            examples = by_endpoint.get(endpoint.id, [])
            if endpoint.api_spec_id is None:
                found.append(MissingEvidence(
                    "endpoint_without_specification",
                    f"{name} is known only from fixtures; no specification describes it.",
                    "legacy", endpoint.id))
            if not examples:
                found.append(MissingEvidence(
                    "endpoint_without_fixture",
                    f"{name} has no historical fixture.", "legacy", endpoint.id))
            documented = {fact.subject for fact in self.session.scalars(
                select(EndpointFact).where(
                    EndpointFact.endpoint_id == endpoint.id,
                    EndpointFact.fact_type == "status_code",
                    EndpointFact.basis == "declared",
                    EndpointFact.evidence_file_id.in_(current_ids)))}
            seen = {str(item.expected_response["status"]) for item in examples}
            for status in sorted(documented - seen):
                if status.isdigit():
                    found.append(MissingEvidence(
                        "status_without_example",
                        f"{name} documents status {status} but no fixture shows it.",
                        "legacy", endpoint.id))

        paired = {item.pair_with for item in fixtures if item.side == "replacement"}
        for fixture in fixtures:
            if fixture.side == "legacy" and fixture.id not in paired:
                found.append(MissingEvidence(
                    "fixture_without_recording",
                    f"Legacy fixture {fixture.name!r} has no paired replacement recording.",
                    "legacy", fixture.endpoint_id))
            if fixture.side == "replacement" and fixture.pair_with_name and not fixture.pair_with:
                found.append(MissingEvidence(
                    "recording_without_fixture",
                    f"Recording {fixture.name!r} is paired with {fixture.pair_with_name!r}, "
                    "which is not a legacy fixture in this project.", "replacement",
                    fixture.endpoint_id))
        return found
