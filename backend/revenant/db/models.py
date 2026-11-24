"""Database models.

Tables are added slice by slice, each with its own migration. Slice 2 creates
the tables that credential storage needs: projects, the two credential tables
and the audit log. See docs/data-model.md for the full design.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from revenant.db.base import Base, new_id, utcnow

PROJECT_STATUSES = ("draft", "evidence", "reconstructed", "adapter_ready", "verified", "running")
INBOUND_KEY_STATUSES = ("active", "revoked")
OUTBOUND_CREDENTIAL_STATUSES = ("active", "previous", "retired")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("slug"),
        CheckConstraint(_in("status", PROJECT_STATUSES), name="status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    legacy_name: Mapped[str] = mapped_column(String(200))
    replacement_name: Mapped[str] = mapped_column(String(200))
    replacement_base_url: Mapped[str | None] = mapped_column(String(2000))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence_version: Mapped[int] = mapped_column(Integer, default=0)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class InboundKey(Base):
    """A legacy-client credential, stored only as a keyed hash."""

    __tablename__ = "inbound_keys"
    __table_args__ = (
        UniqueConstraint("project_id", "label"),
        CheckConstraint(_in("status", INBOUND_KEY_STATUSES), name="status"),
        Index("ix_inbound_keys_project_id_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(64))
    # HMAC-SHA-256 of the key under a key derived from the master key. Not reversible.
    verifier: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundCredential(Base):
    """A replacement-provider credential, encrypted and versioned."""

    __tablename__ = "outbound_credentials"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        CheckConstraint(_in("status", OUTBOUND_CREDENTIAL_STATUSES), name="status"),
        # At most one active version per (project, name). Partial indexes are
        # supported by both SQLite and PostgreSQL.
        Index(
            "uq_outbound_credentials_one_active",
            "project_id",
            "name",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditRecord(Base):
    """Append-only record of who did what. Never holds a credential value."""

    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_records_project_id_created_at", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    # "metadata" is reserved by SQLAlchemy, so the attribute is named "meta".
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
