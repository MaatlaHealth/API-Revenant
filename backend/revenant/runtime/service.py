"""Runtime control: activation, deactivation, legacy hosts, start-up recovery
and the persisted event history (docs/architecture.md, sections 4.3 and 4.4).

"Starting" a runtime opens no socket. It writes the desired state and loads a
compiled adapter into memory. The in-memory state is changed only after the
database transaction has committed, so the two cannot disagree after a failure.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from revenant import audit
from revenant.adapter.validator import Context, validate
from revenant.config import Settings
from revenant.db.base import utcnow
from revenant.db.models import (
    AdapterVersion,
    InboundKey,
    Project,
    RuntimeEvent,
    RuntimeHost,
    RuntimeInstance,
)
from revenant.db.session import Database
from revenant.errors import (
    ApiError,
    Conflict,
    NotConfigured,
    NotFound,
    OutboundBlocked,
    ValidationFailed,
)
from revenant.runtime.hosts import HostError, registrable_host
from revenant.runtime.state import InboundKeys, Loaded, RuntimeState
from revenant.security.credentials import CredentialUnavailable, MasterKeys, OutboundCredentialStore
from revenant.security.outbound import check_url

log = structlog.get_logger("revenant.runtime")


class RuntimeNotReady(ApiError):
    status_code = 409
    code = "runtime_not_ready"


class LoadError(Exception):
    """An adapter cannot be loaded. The message names the reason, never a value."""


def after_commit(session: Session, action: Any) -> None:
    """Run ``action`` once this transaction has committed, and not if it rolls back."""
    event.listen(session, "after_commit", lambda _session: action(), once=True)


def inbound_keys(session: Session, project_id: str) -> InboundKeys:
    rows = session.scalars(select(InboundKey).where(
        InboundKey.project_id == project_id, InboundKey.status == "active")
        .order_by(InboundKey.created_at, InboundKey.label))
    return InboundKeys(tuple((row.label, bytes(row.verifier), row.key_id) for row in rows))


def lifecycle(session: Session, project_id: str, outcome: str, detail: dict[str, Any],
              *, kind: str = "lifecycle", version_id: str | None = None) -> None:
    session.add(RuntimeEvent(project_id=project_id, kind=kind, outcome=outcome, trace=detail,
                             adapter_version_id=version_id))


def load_adapter(session: Session, settings: Settings, keys: MasterKeys | None, project: Project,
                 version: AdapterVersion, generation: int) -> tuple[Loaded, InboundKeys]:
    """Validate a stored document and resolve what it needs. Raises ``LoadError``."""
    document, problems = validate(version.document, Context(
        allow_unauthenticated=project.settings.get("allow_unauthenticated_runtime") is True))
    if document is None or problems:
        raise LoadError("adapter_invalid: " + "; ".join(problems[:5]))
    try:
        check_url(document.upstream.base_url, settings)
    except OutboundBlocked as exc:
        raise LoadError(f"unsafe_upstream: {exc}") from exc
    credential = None
    outbound = document.auth.outbound
    if outbound.type != "none":
        if keys is None:
            raise LoadError("credential_unavailable: no master key is configured")
        try:
            credential = OutboundCredentialStore(session, keys).active(
                project.id, outbound.secret_ref or "")
        except CredentialUnavailable as exc:
            raise LoadError(f"credential_unavailable: {exc}") from exc
    verifiers = inbound_keys(session, project.id)
    if document.auth.inbound.type != "none" and not verifiers.entries:
        raise LoadError("no_active_inbound_key: inbound authentication is declared but no "
                        "legacy-client key is active")
    loaded = Loaded(project_id=project.id, slug=project.slug, version=version.version,
                    version_id=version.id, generation=generation, document=document,
                    credential=credential, loaded_at=utcnow())
    return loaded, verifiers


class RuntimeService:
    def __init__(self, session: Session, settings: Settings, state: RuntimeState) -> None:
        self.session = session
        self.settings = settings
        self.state = state

    def instance(self, project: Project) -> RuntimeInstance:
        row = self.session.scalar(select(RuntimeInstance).where(
            RuntimeInstance.project_id == project.id))
        if row is None:
            row = RuntimeInstance(project_id=project.id)
            self.session.add(row)
            self.session.flush()
        return row

    def _keys(self) -> MasterKeys | None:
        try:
            return self.state.keys or MasterKeys.from_settings(self.settings)
        except NotConfigured:
            return None

    # ------------------------------------------------------------------ start and stop

    def start(self, project: Project, version: int | None, actor: str) -> RuntimeInstance:
        query = select(AdapterVersion).where(AdapterVersion.project_id == project.id)
        query = query.where(AdapterVersion.version == version) if version is not None else (
            query.order_by(AdapterVersion.version.desc()).limit(1))
        adapter = self.session.scalar(query)
        if adapter is None:
            raise RuntimeNotReady(
                "No such adapter version." if version is not None
                else "Compile an adapter before starting the runtime.",
                details=[{"field": "adapter_version", "issue": "no_adapter"}])
        instance = self.instance(project)
        generation = instance.generation + 1
        try:
            loaded, verifiers = load_adapter(self.session, self.settings, self._keys(), project,
                                             adapter, generation)
        except LoadError as exc:
            reason = str(exc)
            raise RuntimeNotReady(
                "The runtime cannot be started.",
                details=[{"field": reason.split(":")[0], "issue": reason}]) from exc
        # One transaction: desired state, pinned version and generation together.
        instance.desired_state, instance.adapter_version_id = "running", adapter.id
        instance.generation, instance.status = generation, "running"
        instance.last_error, instance.last_loaded_at = None, loaded.loaded_at
        if project.status != "running":
            instance.config = {**instance.config, "status_before": project.status}
            project.status = "running"
        lifecycle(self.session, project.id, "started", {"adapter_version": adapter.version,
                                                        "generation": generation},
                  version_id=adapter.id)
        audit.record(self.session, actor=actor, action="runtime.activate",
                     target_type="runtime", target_id=instance.id, project_id=project.id,
                     meta={"adapter_version": adapter.version, "generation": generation})
        self.session.flush()

        def publish() -> None:
            self.state.inbound[project.id] = verifiers
            self.state.slugs[loaded.slug] = project.id
            self.state.status[project.id] = ("running", None)
            self.state.loaded[project.id] = loaded  # one assignment: the swap is atomic

        after_commit(self.session, publish)
        return instance

    def stop(self, project: Project, actor: str) -> RuntimeInstance:
        instance = self.instance(project)
        instance.desired_state, instance.status = "stopped", "stopped"
        instance.generation += 1
        if project.status == "running":
            project.status = instance.config.get("status_before", "adapter_ready")
        lifecycle(self.session, project.id, "stopped", {"generation": instance.generation},
                  version_id=instance.adapter_version_id)
        audit.record(self.session, actor=actor, action="runtime.deactivate",
                     target_type="runtime", target_id=instance.id, project_id=project.id,
                     meta={"generation": instance.generation})
        self.session.flush()
        project_id = project.id

        def publish() -> None:
            self.state.loaded.pop(project_id, None)
            self.state.status[project_id] = ("stopped", None)

        after_commit(self.session, publish)
        return instance

    def reload(self, project: Project) -> None:
        """Reload a running adapter after its credential changed. The new object
        replaces the old one in a single assignment."""
        instance = self.instance(project)
        if instance.desired_state != "running" or instance.adapter_version_id is None:
            return
        adapter = self.session.get(AdapterVersion, instance.adapter_version_id)
        if adapter is None:
            return
        try:
            loaded, verifiers = load_adapter(self.session, self.settings, self._keys(), project,
                                             adapter, instance.generation)
        except LoadError as exc:
            instance.status, instance.last_error = "failed", str(exc)
            project_id, reason = project.id, str(exc)
            lifecycle(self.session, project_id, "load_failed", {"reason": reason}, kind="error")

            def fail() -> None:
                self.state.loaded.pop(project_id, None)
                self.state.status[project_id] = ("failed", reason)

            after_commit(self.session, fail)
            return
        instance.status, instance.last_error = "running", None
        instance.last_loaded_at = loaded.loaded_at
        lifecycle(self.session, project.id, "reloaded", {
            "adapter_version": adapter.version,
            "credential_version": loaded.credential.version if loaded.credential else None},
            version_id=adapter.id)

        def publish() -> None:
            self.state.inbound[project.id] = verifiers
            self.state.status[project.id] = ("running", None)
            self.state.loaded[project.id] = loaded

        after_commit(self.session, publish)

    def refresh_inbound(self, project_id: str) -> None:
        """Make a key change effective for the next request, without a restart."""
        verifiers = inbound_keys(self.session, project_id)
        after_commit(self.session, lambda: self.state.inbound.__setitem__(project_id, verifiers))

    def guard_last_key(self, project: Project, revoking_id: str) -> None:
        """Refuse to revoke the last active key of a running authenticated runtime."""
        loaded = self.state.loaded.get(project.id)
        if loaded is None or loaded.document.auth.inbound.type == "none":
            return
        others = self.session.scalar(select(func.count()).select_from(InboundKey).where(
            InboundKey.project_id == project.id, InboundKey.status == "active",
            InboundKey.id != revoking_id)) or 0
        if others == 0:
            raise Conflict(
                "This is the last active legacy-client key of a running runtime. Add another "
                "key or stop the runtime first.",
                details=[{"field": "key", "issue": "last active key of a running runtime"}])

    # ------------------------------------------------------------------ hosts

    def hosts(self, project_id: str) -> list[str]:
        return list(self.session.scalars(select(RuntimeHost.host).where(
            RuntimeHost.project_id == project_id).order_by(RuntimeHost.host)))

    def set_hosts(self, project: Project, hosts: list[str], actor: str) -> list[str]:
        normalised: list[str] = []
        for index, raw in enumerate(hosts):
            try:
                host = registrable_host(raw, self.settings.public_host_set)
            except HostError as exc:
                raise ValidationFailed(
                    "A legacy host is not acceptable.",
                    details=[{"field": f"legacy_hosts[{index}]", "issue": str(exc)}]) from exc
            if host not in normalised:
                normalised.append(host)
        for host in normalised:
            # The primary key is the normalised form, so case, port, trailing-dot
            # and A-label variants of a registered name all collide here.
            owner = self.session.get(RuntimeHost, host)
            if owner is not None and owner.project_id != project.id:
                raise Conflict("A legacy host is already registered to another project.",
                               details=[{"field": "legacy_hosts", "issue": f"{host} is taken"}])
        for row in self.session.scalars(select(RuntimeHost).where(
                RuntimeHost.project_id == project.id)):
            if row.host not in normalised:
                self.session.delete(row)
        existing = set(self.hosts(project.id))
        for host in normalised:
            if host not in existing:
                self.session.add(RuntimeHost(host=host, project_id=project.id))
        audit.record(self.session, actor=actor, action="runtime.hosts", target_type="runtime",
                     target_id=project.id, project_id=project.id, meta={"hosts": normalised})
        self.session.flush()
        table = host_table(self.session, self.settings)[0]
        after_commit(self.session, lambda: self.state.set_hosts(table))
        return sorted(normalised)

    # ------------------------------------------------------------------ events

    def events(self, project_id: str, *, kind: str | None, outcome: str | None, limit: int,
               offset: int) -> tuple[list[RuntimeEvent], int]:
        """Persisted events only. Dropped traces are not here; their counts are in health."""
        query = select(RuntimeEvent).where(RuntimeEvent.project_id == project_id)
        if kind:
            query = query.where(RuntimeEvent.kind == kind)
        if outcome:
            query = query.where(RuntimeEvent.outcome == outcome)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(query.order_by(
            RuntimeEvent.created_at.desc(), RuntimeEvent.id).limit(limit).offset(offset))
        return list(rows), total

    def event(self, project_id: str, reference: str) -> RuntimeEvent:
        row = self.session.scalar(select(RuntimeEvent).where(
            RuntimeEvent.project_id == project_id,
            (RuntimeEvent.id == reference) | (RuntimeEvent.request_id == reference)))
        if row is None:
            raise NotFound("No stored event with this id. A trace that was dropped is not "
                           "stored.")
        return row


def host_table(session: Session, settings: Settings) -> tuple[dict[str, str], list[str]]:
    """The host table and the registered hosts left out of it because they are
    also management hosts. The management host wins."""
    table: dict[str, str] = {}
    conflicts: list[str] = []
    for row in session.scalars(select(RuntimeHost).order_by(RuntimeHost.host)):
        if row.host in settings.public_host_set:
            conflicts.append(row.host)
        else:
            table[row.host] = row.project_id
    return table, conflicts


def recover(database: Database, settings: Settings, state: RuntimeState) -> None:
    """Start-up recovery. Restores every runtime whose desired state is running, on
    its pinned adapter version. One failure does not stop the others."""
    with database.session() as session:
        table, conflicts = host_table(session, settings)
        state.set_hosts(table)
        state.host_conflicts = tuple(conflicts)
        projects = {project.id: project for project in session.scalars(select(Project))}
        state.slugs = {project.slug: project.id for project in projects.values()}
        for host in conflicts:
            owner = session.get(RuntimeHost, host)
            if owner is not None:
                lifecycle(session, owner.project_id, "host_conflict", {
                    "host": host, "detail": "also a management host; not routed to the runtime"},
                    kind="error")
        for instance in session.scalars(select(RuntimeInstance)):
            project = projects.get(instance.project_id)
            if project is None:
                continue
            if instance.desired_state != "running":
                state.status[project.id] = ("stopped", None)
                continue
            try:
                adapter = session.get(AdapterVersion, instance.adapter_version_id or "")
                if adapter is None:
                    raise LoadError("adapter_missing: the pinned adapter version does not exist")
                loaded, verifiers = load_adapter(session, settings, state.keys, project, adapter,
                                                 instance.generation)
            except Exception as exc:  # noqa: BLE001 - one runtime must not stop the others
                reason = str(exc) if isinstance(exc, LoadError) else (
                    f"load_failed: {type(exc).__name__}")
                instance.status, instance.last_error = "failed", reason
                state.status[project.id] = ("failed", reason)
                lifecycle(session, project.id, "recovery_failed", {"reason": reason},
                          kind="error", version_id=instance.adapter_version_id)
                log.error("runtime_recovery_failed", project=project.slug,
                          reason=reason.split(":")[0])
                continue
            instance.status, instance.last_error = "running", None
            instance.last_loaded_at = loaded.loaded_at
            state.inbound[project.id] = verifiers
            state.status[project.id] = ("running", None)
            state.loaded[project.id] = loaded
            lifecycle(session, project.id, "recovered", {
                "adapter_version": adapter.version, "generation": instance.generation},
                version_id=adapter.id)
            log.info("runtime_recovered", project=project.slug, adapter_version=adapter.version)
    state.recovered = True
