"""Append-only audit records. Metadata must never contain a credential value."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from revenant.db.models import AuditRecord


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    project_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> AuditRecord:
    row = AuditRecord(
        project_id=project_id,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=meta or {},
    )
    session.add(row)
    session.flush()
    return row
