"""Credential endpoints (docs/api-contracts.md, "Credentials").

Legacy-client keys and replacement-provider credentials are separate
resources. No endpoint returns a stored credential value. The only value ever
returned is an inbound key the server generated, once, in the creation response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from revenant import audit
from revenant.api.deps import AdminActor, CurrentProject, DbSession, Keys, require_admin
from revenant.db.models import InboundKey, OutboundCredential
from revenant.security.credentials import InboundKeyStore, OutboundCredentialStore

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/credentials",
    tags=["credentials"],
    # Router-level, so the token is checked before any project is looked up and
    # an unauthenticated caller cannot learn which project ids exist.
    dependencies=[Depends(require_admin)],
)

LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
CredentialName = Annotated[str, Path(pattern=NAME_PATTERN)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InboundKeyCreate(_Body):
    label: str = Field(pattern=LABEL_PATTERN)
    # SecretStr keeps the value out of reprs and validation error output.
    value: SecretStr | None = None


class OutboundCredentialSet(_Body):
    value: SecretStr


class InboundKeyOut(BaseModel):
    id: str
    label: str
    status: str
    created_at: datetime
    revoked_at: datetime | None


class InboundKeyCreated(InboundKeyOut):
    # Present only when the server generated the key. Shown once; not stored.
    generated_value: str | None = None


class OutboundCredentialOut(BaseModel):
    name: str
    version: int
    status: str
    created_at: datetime
    retired_at: datetime | None


def _inbound(row: InboundKey) -> InboundKeyOut:
    return InboundKeyOut(
        id=row.id, label=row.label, status=row.status,
        created_at=row.created_at, revoked_at=row.revoked_at,
    )


def _outbound(row: OutboundCredential) -> OutboundCredentialOut:
    return OutboundCredentialOut(
        name=row.name, version=row.version, status=row.status,
        created_at=row.created_at, retired_at=row.retired_at,
    )


# --------------------------------------------------------------------------- inbound


@router.get("/inbound")
def list_inbound_keys(
    project: CurrentProject, session: DbSession, keys: Keys, _actor: AdminActor
) -> dict[str, object]:
    rows = InboundKeyStore(session, keys).all(project.id)
    return {"items": [_inbound(row) for row in rows], "total": len(rows)}


@router.post("/inbound", status_code=201)
def add_inbound_key(
    body: InboundKeyCreate,
    project: CurrentProject,
    session: DbSession,
    keys: Keys,
    actor: AdminActor,
) -> InboundKeyCreated:
    supplied = body.value.get_secret_value() if body.value is not None else None
    row, generated = InboundKeyStore(session, keys).add(project.id, body.label, supplied)
    audit.record(
        session, actor=actor, action="credential.inbound.add", target_type="inbound_key",
        target_id=row.id, project_id=project.id,
        meta={"label": row.label, "generated": generated is not None},
    )
    return InboundKeyCreated(**_inbound(row).model_dump(), generated_value=generated)


@router.post("/inbound/{key_id}/revoke")
def revoke_inbound_key(
    key_id: str, project: CurrentProject, session: DbSession, keys: Keys, actor: AdminActor
) -> InboundKeyOut:
    row = InboundKeyStore(session, keys).revoke(project.id, key_id)
    audit.record(
        session, actor=actor, action="credential.inbound.revoke", target_type="inbound_key",
        target_id=row.id, project_id=project.id, meta={"label": row.label},
    )
    return _inbound(row)


# --------------------------------------------------------------------------- outbound


@router.get("/outbound")
def list_outbound_credentials(
    project: CurrentProject, session: DbSession, keys: Keys, _actor: AdminActor
) -> dict[str, object]:
    rows = OutboundCredentialStore(session, keys).all(project.id)
    return {"items": [_outbound(row) for row in rows], "total": len(rows)}


@router.put("/outbound/{name}")
def set_outbound_credential(
    name: CredentialName,
    body: OutboundCredentialSet,
    project: CurrentProject,
    session: DbSession,
    keys: Keys,
    actor: AdminActor,
) -> OutboundCredentialOut:
    row = OutboundCredentialStore(session, keys).rotate(
        project.id, name, body.value.get_secret_value()
    )
    audit.record(
        session, actor=actor, action="credential.outbound.rotate",
        target_type="outbound_credential", target_id=row.id, project_id=project.id,
        meta={"name": name, "version": row.version},
    )
    return _outbound(row)


@router.post("/outbound/{name}/rollback")
def rollback_outbound_credential(
    name: CredentialName, project: CurrentProject, session: DbSession, keys: Keys,
    actor: AdminActor,
) -> OutboundCredentialOut:
    row = OutboundCredentialStore(session, keys).rollback(project.id, name)
    audit.record(
        session, actor=actor, action="credential.outbound.rollback",
        target_type="outbound_credential", target_id=row.id, project_id=project.id,
        meta={"name": name, "version": row.version},
    )
    return _outbound(row)


@router.post("/outbound/{name}/versions/{version}/retire")
def retire_outbound_credential(
    name: CredentialName, version: int, project: CurrentProject, session: DbSession, keys: Keys,
    actor: AdminActor,
) -> OutboundCredentialOut:
    row = OutboundCredentialStore(session, keys).retire(project.id, name, version)
    audit.record(
        session, actor=actor, action="credential.outbound.retire",
        target_type="outbound_credential", target_id=row.id, project_id=project.id,
        meta={"name": name, "version": row.version},
    )
    return _outbound(row)
