"""Compile approved mappings into an immutable, versioned Adapter Document, and
dry-run a legacy request through a compiled version without sending anything.

Rules enforced here:
* only mappings with status ``approved`` are read (requirement 4.4);
* a route whose required replacement request fields, or required legacy
  response fields, are not produced is left out and reported, never emitted
  half-working (requirement 5.2);
* the document is statically validated before it is stored (requirement 5.3);
* a route is ``read_only`` only through an approved effect mapping;
* the document holds the name of a credential, never a value (requirement 5.6).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from revenant import audit
from revenant.adapter import SCHEMA_VERSION
from revenant.adapter.document import AdapterDocument, Route
from revenant.adapter.validator import Context, carriers, validate
from revenant.config import Settings
from revenant.db.models import AdapterVersion, Mapping, OutboundCredential, Project
from revenant.errors import ApiError, Conflict, NotFound, OutboundBlocked
from revenant.reconstruction.service import ReconstructionService
from revenant.reconstruction.shapes import FIELD_CATEGORIES
from revenant.security.outbound import check_url
from revenant.transform.engine import TransformError, match_path, request_scopes, transform_request
from revenant.transform.model import CREDENTIAL_HEADERS, SAFE_METHODS

SECTIONS = {"request.body": ("request", "body"), "request.query": ("request", "query"),
            "request.headers": ("request", "headers"), "response.body": ("response", "body"),
            "response.headers": ("response", "headers")}


class AdapterInvalid(ApiError):
    status_code = 422
    code = "adapter_invalid"


class StaleReconstruction(ApiError):
    status_code = 409
    code = "stale_reconstruction"


def canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def route_id(method: str, path: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{method} {path}".lower()).strip("-")[:90] or "route"
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}-{suffix}"
    taken.add(candidate)
    return candidate


def _covered(pointer: str, written: set[str]) -> bool:
    return any(pointer == item or pointer.startswith((item + ".", item + "["))
               for item in written)


def _written(rules: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for rule in rules:
        target = rule.get("to")
        if not isinstance(target, str):
            continue
        found.add(target)
        if rule.get("op") == "each":
            found |= {f"{target}[]{inner[1:]}" for inner in _written(rule.get("rules", []))}
    return found


class AdapterService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.reconstructions = ReconstructionService(session)

    # ------------------------------------------------------------------ compiling

    def _route(self, endpoint: Mapping, group: list[Mapping], pair: dict[str, Any] | None,
               taken: set[str]) -> tuple[dict[str, Any], list[str], list[str]]:
        """One route from the approved mappings of one operation.

        Returns the route, the reasons it cannot be emitted, and the required
        request fields the validator should check.
        """
        match, upstream = endpoint.transformation["match"], endpoint.transformation["upstream"]
        route: dict[str, Any] = {
            "id": route_id(match["method"], match["path"], taken), "mapping_id": endpoint.id,
            "match": dict(match), "upstream": dict(upstream),
            "effect": "unknown" if {match["method"], upstream["method"]} <= SAFE_METHODS
            else "state_changing",
            "request": {"path": [
                {"op": "move", "from": item["from"], "to": f"$.{item['to']}", "required": True,
                 "mapping_id": endpoint.id}
                for item in endpoint.transformation.get("path_params", [])],
                "query": [], "headers": [], "body": {"mode": "build", "rules": []}},
            "response": {"status_map": [], "headers": [],
                         "body": {"mode": "build", "rules": []}, "errors": []},
            "pagination": None,
        }
        loops: dict[tuple[str, str, str], dict[str, Any]] = {}
        for mapping in group:
            data = mapping.transformation
            if mapping.category in FIELD_CATEGORIES:
                side, part = SECTIONS[data["section"]]
                target = route[side][part]["rules"] if part == "body" else route[side][part]
                rule = {**data["rule"], "mapping_id": mapping.id}
                if data.get("each"):
                    key = (data["section"], data["each"]["from"], data["each"]["to"])
                    if key not in loops:
                        loops[key] = {"op": "each", "from": key[1], "to": key[2], "rules": [],
                                      "mapping_id": mapping.id}
                        target.append(loops[key])
                    loops[key]["rules"].append(rule)
                else:
                    target.append(rule)
            elif mapping.category == "status_code":
                route["response"]["status_map"].append(
                    {"from": data["from"], "to": data["to"], "mapping_id": mapping.id})
            elif mapping.category == "error" and data.get("kind") == "error_rule":
                route["response"]["errors"].append(
                    {"when": data["when"], "status": data["status"], "vars": data["vars"],
                     "mapping_id": mapping.id})
            elif mapping.category == "pagination":
                route["pagination"] = {**data, "mapping_id": mapping.id}
            elif mapping.category == "effect":
                route["effect"] = data["effect"]
                route["effect_mapping_id"] = mapping.id

        reasons: list[str] = []
        required: list[str] = []
        same_replacement = pair is not None and (
            pair["replacement"]["method"], pair["replacement"]["path"]) == (
            upstream["method"], upstream["path"])
        if pair is not None and same_replacement:
            required = sorted(item["path"] for item in pair["replacement"]["request_fields"]
                              if item["required"])
            produced = _written(route["request"]["body"]["rules"])
            missing = [pointer for pointer in required if not _covered(pointer, produced)]
            if missing:
                reasons.append("required replacement request fields are not produced: "
                               + ", ".join(missing))
        if pair is not None:
            answered = _written(route["response"]["body"]["rules"])
            paging = route["pagination"]
            if paging:
                answered |= {paging["response"]["items_to"], *paging["response"].get("echo", {})}
                if paging["response"].get("has_more_to"):
                    answered.add(paging["response"]["has_more_to"])
            wanted = sorted({item["path"] for status, items in pair["legacy"]["responses"].items()
                             if status.startswith("2") for item in items if item["required"]})
            absent = [pointer for pointer in wanted if not _covered(pointer, answered)]
            if absent:
                reasons.append("required legacy response fields are not produced: "
                               + ", ".join(absent))
            paginated = any(item.get("pagination_role") == "page"
                            for item in pair["legacy"]["parameters"])
            if paginated and not paging:
                reasons.append("the legacy operation is paginated and no pagination mapping is "
                               "approved")
        return route, reasons, required

    def compile(self, project: Project, actor: str, notes: str = "") -> AdapterVersion:
        latest = self.reconstructions.latest(project.id)
        if latest is None:
            raise Conflict("Run a reconstruction and approve mappings before compiling.",
                           details=[{"field": "reconstruction", "issue": "none exists"}])
        if self.reconstructions.is_stale(project, latest):
            raise StaleReconstruction(
                "Evidence has changed since the last reconstruction. Run it again and review "
                "the new proposals before compiling.")
        # Nothing but approved mappings is read from here on.
        approved = self.reconstructions.mappings(latest.id, status="approved")
        failures: list[str] = []
        warnings: list[str] = []

        auth = [item for item in approved if item.category == "auth"]
        if len(auth) != 1:
            failures.append("auth: no approved authentication mapping")
        if not project.replacement_base_url:
            failures.append("upstream.base_url: the project has no replacement base URL")
        envelope = next((item for item in approved if item.category == "error"
                         and item.transformation.get("kind") == "error_envelope"), None)

        pairs = {pair["legacy"]["endpoint_id"]: pair for pair in latest.report.get("pairs", [])}
        by_operation: dict[str, list[Mapping]] = {}
        for item in approved:
            if item.legacy_endpoint_id and item.category != "endpoint":
                by_operation.setdefault(item.legacy_endpoint_id, []).append(item)
        routes: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        required_fields: dict[str, set[str]] = {}
        used: set[str] = set()
        taken: set[str] = set()
        for endpoint in (item for item in approved if item.category == "endpoint"):
            group = by_operation.get(endpoint.legacy_endpoint_id or "", [])
            route, reasons, required = self._route(
                endpoint, group, pairs.get(endpoint.legacy_endpoint_id or ""), taken)
            if reasons:
                excluded.append({"legacy": endpoint.legacy_ref,
                                 "replacement": endpoint.replacement_ref, "reasons": reasons})
                continue
            routes.append(route)
            required_fields[route["id"]] = set(required)
            used |= {endpoint.id, *(item.id for item in group)}
            if not route["response"]["status_map"]:
                warnings.append(f"{endpoint.legacy_ref}: no approved status mapping; upstream "
                                "success statuses are passed through and flagged")
        routed = {item.legacy_endpoint_id for item in approved if item.category == "endpoint"}
        orphans = sorted({item.legacy_ref for item in approved if item.legacy_endpoint_id
                          and item.legacy_endpoint_id not in routed
                          and item.category != "endpoint"})
        if orphans:
            warnings.append("approved mappings were not compiled because their endpoint mapping "
                            f"is not approved ({len(orphans)} mappings)")
        if not routes and not failures:
            failures.append("routes: no route could be compiled"
                            + ("" if not excluded else " (" + "; ".join(
                                f"{item['legacy']}: {', '.join(item['reasons'])}"
                                for item in excluded) + ")"))
        if failures:
            raise AdapterInvalid("The adapter cannot be compiled.",
                                 details=[{"field": text.split(":")[0], "issue": text}
                                          for text in failures])

        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "project_slug": project.slug,
            "upstream": {"base_url": project.replacement_base_url},
            "auth": {**auth[0].transformation, "mapping_id": auth[0].id},
            "error_envelope": {"mapping_id": envelope.id,
                               "rules": [{**rule, "mapping_id": envelope.id}
                                         for rule in envelope.transformation["rules"]]}
            if envelope else {"rules": []},
            "routes": routes,
        }
        used |= {auth[0].id} | ({envelope.id} if envelope else set())
        base_url_problem = None
        try:
            check_url(project.replacement_base_url or "", self.settings)
        except OutboundBlocked as exc:
            base_url_problem = str(exc)
        _, problems = validate(document, Context(
            approved_mapping_ids={item.id for item in approved},
            required_request_fields=required_fields, base_url_problem=base_url_problem,
            allow_unauthenticated=project.settings.get("allow_unauthenticated_runtime") is True))
        if problems:
            raise AdapterInvalid("The compiled adapter failed static validation.",
                                 details=[{"field": text.split(":")[0], "issue": text}
                                          for text in problems])
        secret = document["auth"]["outbound"].get("secret_ref")
        if secret and not self.session.scalar(select(OutboundCredential.id).where(
                OutboundCredential.project_id == project.id, OutboundCredential.name == secret,
                OutboundCredential.status == "active")):
            warnings.append(f"no active replacement credential named {secret} is stored yet; "
                            "the runtime cannot start until one is")
        if not envelope and any(route["response"]["errors"] for route in routes):
            warnings.append("error rules are approved but the error envelope is not; error "
                            "bodies will be empty")

        version = int(self.session.scalar(
            select(func.coalesce(func.max(AdapterVersion.version), 0))
            .where(AdapterVersion.project_id == project.id)) or 0) + 1
        record = AdapterVersion(
            project_id=project.id, version=version, reconstruction_id=latest.id,
            document=document,
            document_sha256=hashlib.sha256(canonical(document).encode("utf-8")).hexdigest(),
            mapping_ids=sorted(used),
            report={"routes": len(routes), "excluded_routes": excluded, "warnings": warnings,
                    "approved_mappings": len(approved), "compiled_mappings": len(used)},
            compiled_by=actor, notes=notes)
        self.session.add(record)
        if project.status in ("draft", "evidence", "reconstructed"):
            project.status = "adapter_ready"
        self.session.flush()
        audit.record(self.session, actor=actor, action="adapter.compile",
                     target_type="adapter_version", target_id=record.id, project_id=project.id,
                     meta={"version": version, "routes": len(routes),
                           "excluded_routes": len(excluded),
                           "document_sha256": record.document_sha256})
        return record

    # ------------------------------------------------------------------ reading

    def versions(self, project_id: str) -> list[AdapterVersion]:
        return list(self.session.scalars(
            select(AdapterVersion).where(AdapterVersion.project_id == project_id)
            .order_by(AdapterVersion.version.desc())))

    def get(self, project_id: str, version: int) -> AdapterVersion:
        record = self.session.scalar(select(AdapterVersion).where(
            AdapterVersion.project_id == project_id, AdapterVersion.version == version))
        if record is None:
            raise NotFound("Adapter version not found.")
        return record

    # ------------------------------------------------------------------ dry run

    def dry_run(self, project: Project, version: int, method: str, path: str,
                query: dict[str, Any], headers: dict[str, Any], body: Any) -> dict[str, Any]:
        """Transform one legacy request and return what would be sent. Sends nothing."""
        record = self.get(project.id, version)
        document, problems = validate(record.document, Context(
            allow_unauthenticated=project.settings.get("allow_unauthenticated_runtime") is True))
        if document is None or problems:
            raise AdapterInvalid("The stored adapter failed validation when loaded.",
                                 details=[{"field": "document", "issue": text}
                                          for text in problems])
        clean_headers, clean_query, presented = strip_credentials(document, headers, query)
        result: dict[str, Any] = {
            "adapter_version": record.version, "sent": False,
            "auth": {"inbound": {"type": document.auth.inbound.type,
                                 "credential": "present" if presented else "absent",
                                 "verified": False},
                     "outbound": {"type": document.auth.outbound.type,
                                  "secret_ref": document.auth.outbound.secret_ref,
                                  "attached": False}},
        }
        route = find_route(document, method, path)
        if route is None:
            return {**result, "matched": False, "outcome": "no_route", "legacy_status": 404}
        result.update(matched=True, route_id=route.id, effect=route.effect)
        try:
            upstream = transform_request(
                route, request_scopes(route, path, clean_query, clean_headers, body))
        except TransformError as exc:
            return {**result, "outcome": "transform_error", "legacy_status": 400,
                    "error": {"code": exc.code, "message": exc.message,
                              "rule_index": exc.rule_index}}
        produced = {"method": upstream.method, "path": upstream.path, "query": upstream.query,
                    "url": document.upstream.base_url.rstrip("/") + upstream.path,
                    "headers": upstream.headers, "body": upstream.body}
        if any(value and value in json.dumps(produced) for value in presented):
            # The legacy credential must never travel upstream, however it got there.
            return {**result, "outcome": "credential_leak_blocked", "legacy_status": 500}
        return {**result, "outcome": "ok", "upstream": produced, "trace": upstream.trace,
                "unmapped_fields": upstream.unmapped_fields}


def strip_credentials(document: AdapterDocument, headers: dict[str, Any],
                      query: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Remove every credential carrier before any rule can see the request.

    Returns the cleaned headers and query and the legacy credential values that
    were presented (kept only to check that none of them reaches the output).
    """
    read_headers, read_query, _, _ = carriers(document)
    inbound = document.auth.inbound
    presented: list[str] = []
    clean_headers: dict[str, Any] = {}
    for name, value in headers.items():
        lowered = str(name).lower()
        if inbound.type == "api_key_header" and lowered == (inbound.header or "").lower():
            presented.append(str(value))
        elif inbound.type in ("bearer", "basic") and lowered == "authorization":
            presented.append(str(value).partition(" ")[2] or str(value))
        if lowered not in read_headers and lowered not in CREDENTIAL_HEADERS:
            clean_headers[name] = value
    clean_query: dict[str, Any] = {}
    for name, value in query.items():
        if str(name).lower() in read_query:
            presented.append(str(value))
        else:
            clean_query[name] = value
    return clean_headers, clean_query, [value for value in presented if value]


def find_route(document: AdapterDocument, method: str, path: str) -> Route | None:
    """The route for a legacy request; literal path segments win over parameters."""
    best: tuple[int, Route] | None = None
    for route in document.routes:
        if route.match.method != method.upper() or match_path(route.match.path, path) is None:
            continue
        literal = sum(1 for part in route.match.path.split("/") if part and "{" not in part)
        if best is None or literal > best[0]:
            best = (literal, route)
    return best[1] if best else None
