"""Runtime control and the persisted event history
(docs/api-contracts.md, "Runtime control")."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from revenant.api.deps import AdminActor, CurrentProject, DbSession, get_settings, require_admin
from revenant.config import Settings
from revenant.db.models import AdapterVersion, Project, RuntimeEvent
from revenant.runtime.proxy import RuntimeProxy
from revenant.runtime.service import RuntimeService
from revenant.runtime.state import RuntimeState

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["runtime"],
                   dependencies=[Depends(require_admin)])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_state(request: Request) -> RuntimeState:
    state: RuntimeState = request.app.state.runtime
    return state


def get_proxy(request: Request) -> RuntimeProxy:
    proxy: RuntimeProxy = request.app.state.proxy
    return proxy


State = Annotated[RuntimeState, Depends(get_state)]
Proxy = Annotated[RuntimeProxy, Depends(get_proxy)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRequest(_Body):
    adapter_version: int | None = Field(default=None, ge=1)


class RuntimeUpdate(_Body):
    legacy_hosts: list[str] = Field(max_length=50)


def _describe(service: RuntimeService, project: Project, proxy: RuntimeProxy) -> dict[str, Any]:
    instance = service.instance(project)
    adapter = service.session.get(AdapterVersion, instance.adapter_version_id or "")
    return {
        "desired_state": instance.desired_state, "status": instance.status,
        "adapter_version": adapter.version if adapter else None,
        "generation": instance.generation, "last_error": instance.last_error,
        "last_loaded_at": instance.last_loaded_at,
        "legacy_hosts": service.hosts(project.id),
        "path_entry": f"/runtime/{project.slug}/",
        "health": proxy.health(project.id),
    }


def _event(row: RuntimeEvent, *, detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id, "request_id": row.request_id, "kind": row.kind, "entry": row.entry,
        "original_host": row.original_host, "method": row.method,
        "legacy_path": row.legacy_path, "legacy_query": row.legacy_query,
        "query_credential_removed": row.query_credential_removed,
        "upstream_path": row.upstream_path, "route_id": row.route_id,
        "legacy_status": row.legacy_status, "upstream_status": row.upstream_status,
        "duration_ms": row.duration_ms, "upstream_ms": row.upstream_ms, "outcome": row.outcome,
        "adapter_version_id": row.adapter_version_id, "created_at": row.created_at,
    }
    if detail or row.kind != "request":
        out["trace"] = row.trace
    return out


@router.get("/runtime")
def get_runtime(project: CurrentProject, session: DbSession, settings: SettingsDep, state: State,
                proxy: Proxy) -> dict[str, Any]:
    return _describe(RuntimeService(session, settings, state), project, proxy)


@router.post("/runtime/start")
def start_runtime(project: CurrentProject, session: DbSession, settings: SettingsDep,
                  state: State, proxy: Proxy, actor: AdminActor,
                  body: StartRequest | None = None) -> dict[str, Any]:
    service = RuntimeService(session, settings, state)
    service.start(project, body.adapter_version if body else None, actor)
    return _describe(service, project, proxy)


@router.post("/runtime/stop")
def stop_runtime(project: CurrentProject, session: DbSession, settings: SettingsDep,
                 state: State, proxy: Proxy, actor: AdminActor) -> dict[str, Any]:
    service = RuntimeService(session, settings, state)
    service.stop(project, actor)
    return _describe(service, project, proxy)


@router.patch("/runtime")
def update_runtime(body: RuntimeUpdate, project: CurrentProject, session: DbSession,
                   settings: SettingsDep, state: State, proxy: Proxy,
                   actor: AdminActor) -> dict[str, Any]:
    service = RuntimeService(session, settings, state)
    service.set_hosts(project, body.legacy_hosts, actor)
    return _describe(service, project, proxy)


@router.get("/runtime/events")
def list_events(
    project: CurrentProject, session: DbSession, settings: SettingsDep, state: State,
    kind: Annotated[Literal["request", "lifecycle", "error", "trace_loss"] | None, Query()] = None,
    outcome: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows, total = RuntimeService(session, settings, state).events(
        project.id, kind=kind, outcome=outcome, limit=limit, offset=offset)
    return {"items": [_event(row) for row in rows], "total": total}


@router.get("/runtime/events/{event_id}")
def get_event(event_id: str, project: CurrentProject, session: DbSession, settings: SettingsDep,
              state: State) -> dict[str, Any]:
    return _event(RuntimeService(session, settings, state).event(project.id, event_id),
                  detail=True)
