"""Shared dependencies for management API routes."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from revenant.config import Settings
from revenant.db.models import Project
from revenant.db.session import Database
from revenant.errors import NotFound, Unauthenticated
from revenant.security.credentials import MasterKeys

# The management API is single-tenant behind one token (docs/security.md).
ADMIN_ACTOR = "admin"


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_session(database: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    """One unit of work per request: commit on success, roll back on error.

    Used with ``scope="function"`` so the commit happens before the response is
    sent. A client that acts on a response always finds its change stored.
    """
    with database.session() as session:
        yield session


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Require ``Authorization: Bearer <admin token>``. Returns the actor label.

    The response is the same whether the header is missing, malformed or wrong,
    and whether or not a token is configured at all.
    """
    expected = settings.admin_token.get_secret_value()
    presented = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    # Constant-time comparison. An unset admin token never matches anything.
    matches = hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    if not expected or not presented or not matches:
        raise Unauthenticated("A valid admin token is required.")
    return ADMIN_ACTOR


def get_master_keys(settings: Annotated[Settings, Depends(get_settings)]) -> MasterKeys:
    return MasterKeys.from_settings(settings)


def get_project(
    project_id: str,
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound("Project not found.")
    return project


AdminActor = Annotated[str, Depends(require_admin)]
DbSession = Annotated[Session, Depends(get_session, scope="function")]
CurrentProject = Annotated[Project, Depends(get_project)]
Keys = Annotated[MasterKeys, Depends(get_master_keys)]
