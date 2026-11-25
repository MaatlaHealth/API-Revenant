"""Running a reconstruction, storing it, and reviewing what it proposed.

Rules enforced here:
* a reconstruction and its inventory are written once; later evidence enters an
  inventory only through a new reconstruction;
* staleness is computed from fingerprints, never stored;
* every review decision is an append-only row with actor, note and time, and an
  audit record;
* only the latest reconstruction can be reviewed; earlier ones are read-only;
* an operation is read-only only by an explicit human declaration with a note,
  and never when a method is unsafe or evidence marks it state-changing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from revenant import audit
from revenant.db.models import (
    Behaviour,
    Endpoint,
    EndpointFact,
    EvidenceFile,
    Gap,
    Invariant,
    Mapping,
    MappingDecision,
    Project,
    Reconstruction,
)
from revenant.errors import ApiError, Conflict, NotFound, ValidationFailed
from revenant.evidence.service import current_files
from revenant.reconstruction import ENGINE_VERSION, inventory
from revenant.reconstruction.contract import load_contracts
from revenant.reconstruction.effects import effective_effect, read_only_objections
from revenant.reconstruction.engine import reconstruct
from revenant.reconstruction.shapes import validate_transformation

LIVE = ("proposed", "approved", "rejected")  # statuses of a mapping that is not superseded


class Immutable(ApiError):
    status_code = 409
    code = "immutable"


class ReconstructionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ running

    def run(self, project: Project, actor: str) -> Reconstruction:
        contracts, files = load_contracts(self.session, project.id)
        sides = {item.side for item in contracts}
        if sides != {"legacy", "replacement"}:
            raise ValidationFailed(
                "Reconstruction needs endpoints on both sides.",
                details=[{"field": "evidence", "issue": f"no {side} endpoints have been imported"}
                         for side in ("legacy", "replacement") if side not in sides])
        result = reconstruct(contracts, files)
        previous = self.latest(project.id)
        keys = [item.key for item in result.behaviours]
        record = Reconstruction(
            project_id=project.id, sequence=(previous.sequence + 1) if previous else 1,
            status="completed", engine_version=ENGINE_VERSION, summary=result.summary,
            report=result.report, evidence_version=project.evidence_version,
            evidence_fingerprint=project.evidence_fingerprint,
            evidence_file_ids=sorted(item.id for item in files),
            inventory_size=len(keys), inventory_fingerprint=inventory.fingerprint(keys),
            previous_reconstruction_id=previous.id if previous else None,
            inventory_diff=inventory.difference(
                self._behaviour_keys(previous.id) if previous else None, keys),
            created_by=actor,
        )
        self.session.add(record)
        self.session.flush()
        owner = {"project_id": project.id, "reconstruction_id": record.id}
        for position, proposal in enumerate(result.proposals):
            self.session.add(Mapping(
                **owner, key=proposal.key, position=position,
                legacy_endpoint_id=proposal.legacy_endpoint_id,
                replacement_endpoint_id=proposal.replacement_endpoint_id,
                category=proposal.category, legacy_ref=proposal.legacy_ref[:700],
                replacement_ref=proposal.replacement_ref[:700],
                transformation=proposal.transformation, confidence=proposal.confidence,
                rule_name=proposal.rule_name, rationale=proposal.rationale,
                evidence_refs=proposal.evidence_refs,
                requires_approval=proposal.requires_approval, status="proposed", origin="engine"))
        for gap in result.gaps:
            self.session.add(Gap(
                **owner, key=gap.key, endpoint_id=gap.endpoint_id, endpoint_ref=gap.endpoint_ref,
                category=gap.category, title=gap.title[:500], description=gap.description,
                evidence_refs=gap.evidence_refs, missing=gap.missing, severity=gap.severity,
                behaviour_key=gap.behaviour_key or f"gap: {gap.key}", resolution="open"))
        for invariant in result.invariants:
            self.session.add(Invariant(
                **owner, key=invariant.key, endpoint_id=invariant.endpoint_id,
                endpoint_ref=invariant.endpoint_ref, kind=invariant.kind,
                definition=invariant.definition, confidence=invariant.confidence,
                evidence_refs=invariant.evidence_refs,
                # An inferred invariant is not used until a person approves it.
                status="active" if invariant.confidence == "observed" else "proposed"))
        for behaviour in result.behaviours:
            self.session.add(Behaviour(
                **owner, endpoint_id=behaviour.endpoint_id, endpoint_ref=behaviour.endpoint_ref,
                kind=behaviour.kind, key=behaviour.key, description=behaviour.description,
                evidence_refs=behaviour.evidence_refs, scope="in_scope",
                unsupported_reason=behaviour.unsupported_reason))
        if project.status in ("draft", "evidence"):
            project.status = "reconstructed"
        self.session.flush()
        audit.record(self.session, actor=actor, action="reconstruction.run",
                     target_type="reconstruction", target_id=record.id, project_id=project.id,
                     meta={"sequence": record.sequence,
                           "evidence_version": record.evidence_version,
                           "mappings": len(result.proposals), "gaps": len(result.gaps),
                           "inventory_size": record.inventory_size})
        return record

    # ------------------------------------------------------------------ reading

    def latest(self, project_id: str) -> Reconstruction | None:
        return self.session.scalar(
            select(Reconstruction).where(Reconstruction.project_id == project_id)
            .order_by(Reconstruction.sequence.desc()).limit(1))

    def require_latest(self, project_id: str) -> Reconstruction:
        record = self.latest(project_id)
        if record is None:
            raise NotFound("No reconstruction has been run for this project.")
        return record

    def get(self, project_id: str, reconstruction_id: str) -> Reconstruction:
        record = self.session.get(Reconstruction, reconstruction_id)
        if record is None or record.project_id != project_id:
            raise NotFound("Reconstruction not found.")
        return record

    def history(self, project_id: str) -> list[Reconstruction]:
        return list(self.session.scalars(
            select(Reconstruction).where(Reconstruction.project_id == project_id)
            .order_by(Reconstruction.sequence.desc())))

    @staticmethod
    def is_stale(project: Project, record: Reconstruction) -> bool:
        """Computed by comparison every time, so it cannot be forgotten."""
        return record.evidence_fingerprint != project.evidence_fingerprint

    def unanalysed_files(self, project: Project, record: Reconstruction) -> int:
        analysed = set(record.evidence_file_ids)
        return sum(1 for item in current_files(self.session, project.id)
                   if item.id not in analysed)

    def _behaviour_keys(self, reconstruction_id: str) -> list[str]:
        return list(self.session.scalars(
            select(Behaviour.key).where(Behaviour.reconstruction_id == reconstruction_id)))

    def filenames(self, project_id: str) -> dict[str, tuple[str, bool]]:
        """Evidence file id -> (filename, removed). Includes removed files."""
        return {item.id: (item.filename, item.deleted_at is not None)
                for item in self.session.scalars(
                    select(EvidenceFile).where(EvidenceFile.project_id == project_id))}

    # ------------------------------------------------------------------ mappings

    def mappings(self, reconstruction_id: str, *, status: str | None = None,
                 category: str | None = None, confidence: str | None = None,
                 include_superseded: bool = False) -> list[Mapping]:
        query = select(Mapping).where(Mapping.reconstruction_id == reconstruction_id)
        if status:
            query = query.where(Mapping.status == status)
        elif not include_superseded:
            query = query.where(Mapping.status.in_(LIVE))
        if category:
            query = query.where(Mapping.category == category)
        if confidence:
            query = query.where(Mapping.confidence == confidence)
        return list(self.session.scalars(
            query.order_by(Mapping.position, Mapping.created_at, Mapping.id)))

    def mapping(self, project_id: str, mapping_id: str) -> Mapping:
        row = self.session.get(Mapping, mapping_id)
        if row is None or row.project_id != project_id:
            raise NotFound("Mapping not found.")
        return row

    def decisions(self, mapping_id: str) -> list[MappingDecision]:
        return list(self.session.scalars(
            select(MappingDecision).where(MappingDecision.mapping_id == mapping_id)
            .order_by(MappingDecision.created_at, MappingDecision.id)))

    def _reviewable(self, project: Project, reconstruction_id: str) -> None:
        latest = self.latest(project.id)
        if latest is None or latest.id != reconstruction_id:
            raise Immutable("This belongs to an earlier reconstruction, which is read-only. "
                            "Review the latest reconstruction instead.")

    def _decide(self, mapping: Mapping, decision: str, actor: str, note: str) -> None:
        self.session.add(MappingDecision(mapping_id=mapping.id, decision=decision, actor=actor,
                                         note=note))

    def decide(self, project: Project, mapping: Mapping, decision: str, actor: str,
               note: str = "") -> Mapping:
        """Approve or reject a mapping. ``decision`` is ``approved`` or ``rejected``."""
        self._reviewable(project, mapping.reconstruction_id)
        if mapping.status == "superseded":
            raise Conflict("This mapping was superseded by an edit and can no longer be decided.",
                           details=[{"field": "status", "issue": "superseded"}])
        if decision == "approved" and mapping.category == "effect" \
                and mapping.transformation.get("effect") == "read_only":
            self._check_read_only(project, mapping)
        mapping.status = decision
        self._decide(mapping, decision, actor, note)
        self.session.flush()
        audit.record(
            self.session, actor=actor,
            action="mapping.approve" if decision == "approved" else "mapping.reject",
            target_type="mapping", target_id=mapping.id, project_id=project.id,
            meta={"key": mapping.key, "category": mapping.category,
                  "confidence": mapping.confidence, "note": note})
        return mapping

    def edit(self, project: Project, mapping: Mapping, transformation: Any, actor: str,
             note: str = "") -> Mapping:
        """Store an edited rule as a new human mapping that supersedes the proposal."""
        self._reviewable(project, mapping.reconstruction_id)
        if mapping.status == "superseded":
            raise Conflict("This mapping has already been superseded by an edit.",
                           details=[{"field": "status", "issue": "superseded"}])
        if mapping.category == "effect":
            raise ValidationFailed(
                "An operation's effect is declared through the operations endpoint.",
                details=[{"field": "transformation", "issue": "effect mappings are not edited"}])
        problems = validate_transformation(mapping.category, transformation)
        replacement_id, replacement_ref = mapping.replacement_endpoint_id, mapping.replacement_ref
        if not problems and mapping.category == "endpoint":
            match, upstream = transformation["match"], transformation["upstream"]
            legacy = self.session.get(Endpoint, mapping.legacy_endpoint_id or "")
            if legacy is None or (match["method"], match["path"]) != (
                    legacy.method, legacy.path_template):
                problems.append("match: the legacy side of an endpoint mapping cannot be changed")
            target = self.session.scalar(select(Endpoint).where(
                Endpoint.project_id == project.id, Endpoint.side == "replacement",
                Endpoint.method == upstream["method"],
                Endpoint.path_template == upstream["path"]))
            if target is None:
                problems.append("upstream: no such endpoint in the imported replacement evidence")
            else:
                replacement_id = target.id
                replacement_ref = f"{target.method} {target.path_template}"
        if problems:
            raise ValidationFailed(
                "The edited transformation is not valid.",
                details=[{"field": "transformation", "issue": text} for text in problems])
        replacement = Mapping(
            project_id=project.id, reconstruction_id=mapping.reconstruction_id, key=mapping.key,
            position=mapping.position, legacy_endpoint_id=mapping.legacy_endpoint_id,
            replacement_endpoint_id=replacement_id, category=mapping.category,
            legacy_ref=mapping.legacy_ref, replacement_ref=replacement_ref,
            transformation=transformation,
            # A person wrote it; no imported evidence is claimed for it.
            confidence="inferred", rule_name="human_edit",
            rationale=note or f"Edited by {actor}.", evidence_refs=[], requires_approval=True,
            status="proposed", origin="human", supersedes_id=mapping.id)
        mapping.status = "superseded"
        self._decide(mapping, "edited", actor, note)
        self.session.add(replacement)
        self.session.flush()
        audit.record(self.session, actor=actor, action="mapping.edit", target_type="mapping",
                     target_id=replacement.id, project_id=project.id,
                     meta={"key": mapping.key, "category": mapping.category,
                           "supersedes": mapping.id, "note": note})
        return replacement

    def bulk_approve(self, project: Project, actor: str, *, ids: list[str] | None,
                     category: str | None, note: str = "") -> dict[str, Any]:
        """Approve listed mappings, or every proposed ``observed`` mapping.

        The filter form never touches an inferred mapping: those are approved
        one at a time, by id.
        """
        latest = self.require_latest(project.id)
        skipped: list[dict[str, str]] = []
        if ids is not None:
            rows = []
            for mapping_id in ids:
                row = self.session.get(Mapping, mapping_id)
                if row is None or row.project_id != project.id:
                    raise NotFound(f"Mapping {mapping_id} not found.")
                if row.reconstruction_id != latest.id:
                    skipped.append({"id": row.id, "reason": "belongs to an earlier reconstruction"})
                elif row.status != "proposed":
                    skipped.append({"id": row.id, "reason": f"status is {row.status}"})
                else:
                    rows.append(row)
        else:
            rows = [row for row in self.mappings(latest.id, status="proposed", category=category)
                    if row.confidence == "observed" and not row.requires_approval]
        for row in rows:
            self.decide(project, row, "approved", actor, note)
        return {"approved": rows, "skipped": skipped}

    # ------------------------------------------------------------------ operations

    def _markers(self, project_id: str, endpoint_ids: list[str]) -> list[tuple[str, str]]:
        current = [item.id for item in current_files(self.session, project_id)]
        if not current or not endpoint_ids:
            return []
        rows = self.session.execute(
            select(Endpoint, EndpointFact)
            .join(EndpointFact, EndpointFact.endpoint_id == Endpoint.id)
            .where(EndpointFact.fact_type == "effect", Endpoint.id.in_(endpoint_ids),
                   EndpointFact.evidence_file_id.in_(current))
            .order_by(Endpoint.side, EndpointFact.locator)).all()
        return [(f"{endpoint.method} {endpoint.path_template}", str(fact.value.get("effect")))
                for endpoint, fact in rows]

    def _check_read_only(self, project: Project, route_or_effect: Mapping) -> None:
        route = self._route(route_or_effect.reconstruction_id,
                            route_or_effect.legacy_endpoint_id or "")
        problems = ["the operation has no endpoint mapping"] if route is None else \
            read_only_objections(
                route.transformation["match"]["method"],
                route.transformation["upstream"]["method"],
                self._markers(project.id, [route.legacy_endpoint_id or "",
                                           route.replacement_endpoint_id or ""]))
        if problems:
            raise ValidationFailed(
                "This operation cannot be declared read-only.",
                details=[{"field": "effect", "issue": text} for text in problems])

    def _route(self, reconstruction_id: str, legacy_endpoint_id: str) -> Mapping | None:
        """The current endpoint mapping of an operation (approved one first)."""
        rows = [row for row in self.mappings(reconstruction_id, category="endpoint")
                if row.legacy_endpoint_id == legacy_endpoint_id and row.status != "rejected"]
        rows.sort(key=lambda row: (row.status != "approved", row.created_at))
        return rows[0] if rows else None

    def _effect_mapping(self, reconstruction_id: str, legacy_endpoint_id: str) -> Mapping | None:
        rows = [row for row in self.mappings(reconstruction_id, category="effect")
                if row.legacy_endpoint_id == legacy_endpoint_id]
        rows.sort(key=lambda row: (row.status != "approved", row.created_at))
        return rows[0] if rows else None

    def operations(self, project: Project) -> list[dict[str, Any]]:
        latest = self.require_latest(project.id)
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for route in self.mappings(latest.id, category="endpoint"):
            operation_id = route.legacy_endpoint_id
            if operation_id is None or operation_id in seen or route.status == "rejected":
                continue
            current = self._route(latest.id, operation_id)
            if current is None:
                continue
            seen.add(operation_id)
            match, upstream = current.transformation["match"], current.transformation["upstream"]
            effect = self._effect_mapping(latest.id, operation_id)
            value = effect.transformation.get("effect") if effect else None
            declared = value if effect and effect.status == "approved" else None
            decision = self.decisions(effect.id)[-1] if effect and declared else None
            found.append({
                "id": operation_id,
                "legacy": {"method": match["method"], "path": match["path"]},
                "replacement": {"method": upstream["method"], "path": upstream["path"]},
                "endpoint_mapping": {"id": current.id, "status": current.status,
                                     "confidence": current.confidence},
                "proposed_effect": value if effect and effect.status == "proposed" else None,
                "declared_effect": declared,
                "effective_effect": effective_effect(match["method"], upstream["method"],
                                                     declared),
                "declared_by": decision.actor if decision else None,
                "declared_at": decision.created_at if decision else None,
                "note": decision.note if decision else None,
                "effect_mapping_id": effect.id if effect else None,
                "origin": effect.origin if effect else None,
                "rationale": effect.rationale if effect else None,
                "evidence_markers": [
                    {"endpoint": name, "effect": marked} for name, marked in self._markers(
                        project.id, [operation_id, current.replacement_endpoint_id or ""])],
            })
        return found

    def declare_effect(self, project: Project, operation_id: str, effect: str, note: str,
                       actor: str) -> Mapping:
        """An explicit human declaration. Always recorded with actor and note."""
        latest = self.require_latest(project.id)
        route = self._route(latest.id, operation_id)
        if route is None:
            raise NotFound("Operation not found in the latest reconstruction.")
        if not note.strip():
            raise ValidationFailed("A note is required when declaring an operation's effect.",
                                   details=[{"field": "note", "issue": "must not be empty"}])
        if effect == "read_only":
            self._check_read_only(project, route)
        previous = self._effect_mapping(latest.id, operation_id)
        declared = Mapping(
            project_id=project.id, reconstruction_id=latest.id,
            key=previous.key if previous else f"effect:{route.legacy_ref}",
            position=previous.position if previous else route.position,
            legacy_endpoint_id=operation_id,
            replacement_endpoint_id=route.replacement_endpoint_id, category="effect",
            legacy_ref=route.legacy_ref, replacement_ref=route.replacement_ref,
            transformation={"effect": effect}, confidence="inferred",
            rule_name="human_declaration", rationale=note.strip(), evidence_refs=[],
            requires_approval=True, status="approved", origin="human",
            supersedes_id=previous.id if previous else None)
        for row in self.mappings(latest.id, category="effect"):
            if row.legacy_endpoint_id == operation_id:
                row.status = "superseded"
                self._decide(row, "edited", actor, note.strip())
        self.session.add(declared)
        self.session.flush()
        self._decide(declared, "approved", actor, note.strip())
        self.session.flush()
        audit.record(self.session, actor=actor, action="operation.effect.declare",
                     target_type="mapping", target_id=declared.id, project_id=project.id,
                     meta={"effect": effect, "legacy": route.legacy_ref,
                           "replacement": route.replacement_ref, "note": note.strip()})
        return declared

    # ------------------------------------------------------------------ gaps, invariants

    def gaps(self, reconstruction_id: str) -> list[Gap]:
        return list(self.session.scalars(
            select(Gap).where(Gap.reconstruction_id == reconstruction_id)
            .order_by(func.coalesce(Gap.endpoint_ref, ""), Gap.key)))

    def resolve_gap(self, project: Project, gap_id: str, resolution: str, note: str,
                    actor: str) -> Gap:
        gap = self.session.get(Gap, gap_id)
        if gap is None or gap.project_id != project.id:
            raise NotFound("Gap not found.")
        self._reviewable(project, gap.reconstruction_id)
        if resolution == "accepted_risk" and not note.strip():
            raise ValidationFailed("A note is required to accept a gap as a risk.",
                                   details=[{"field": "note", "issue": "must not be empty"}])
        gap.resolution, gap.resolution_note = resolution, note.strip()
        self.session.flush()
        audit.record(self.session, actor=actor, action="gap.resolve", target_type="gap",
                     target_id=gap.id, project_id=project.id,
                     meta={"key": gap.key, "resolution": resolution, "note": note.strip()})
        return gap

    def invariants(self, reconstruction_id: str) -> list[Invariant]:
        return list(self.session.scalars(
            select(Invariant).where(Invariant.reconstruction_id == reconstruction_id)
            .order_by(Invariant.endpoint_ref, Invariant.kind, Invariant.key)))

    def decide_invariant(self, project: Project, invariant_id: str, approve: bool, actor: str,
                         note: str = "") -> Invariant:
        row = self.session.get(Invariant, invariant_id)
        if row is None or row.project_id != project.id:
            raise NotFound("Invariant not found.")
        self._reviewable(project, row.reconstruction_id)
        row.status = "active" if approve else "rejected"
        self.session.flush()
        audit.record(self.session, actor=actor,
                     action="invariant.approve" if approve else "invariant.reject",
                     target_type="invariant", target_id=row.id, project_id=project.id,
                     meta={"key": row.key, "confidence": row.confidence, "note": note})
        return row

    # ------------------------------------------------------------------ behaviours

    def behaviours(self, reconstruction_id: str) -> list[Behaviour]:
        return list(self.session.scalars(
            select(Behaviour).where(Behaviour.reconstruction_id == reconstruction_id)
            .order_by(func.coalesce(Behaviour.endpoint_ref, "~"), Behaviour.kind, Behaviour.key)))

    def set_scope(self, project: Project, behaviour_id: str, scope: str, note: str,
                  actor: str) -> Behaviour:
        """Mark a behaviour in or out of scope. It never leaves the inventory."""
        row = self.session.get(Behaviour, behaviour_id)
        if row is None or row.project_id != project.id:
            raise NotFound("Behaviour not found.")
        self._reviewable(project, row.reconstruction_id)
        if scope == "out_of_scope" and not note.strip():
            raise ValidationFailed("A note is required to mark a behaviour out of scope.",
                                   details=[{"field": "note", "issue": "must not be empty"}])
        row.scope, row.scope_note = scope, note.strip()
        self.session.flush()
        audit.record(self.session, actor=actor, action="behaviour.scope",
                     target_type="behaviour", target_id=row.id, project_id=project.id,
                     meta={"key": row.key, "scope": scope, "note": note.strip()})
        return row
