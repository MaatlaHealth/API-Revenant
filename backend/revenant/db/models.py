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

from revenant.db.base import Base, PortableJSON, new_id, utcnow

PROJECT_STATUSES = ("draft", "evidence", "reconstructed", "adapter_ready", "verified", "running")
SIDES = ("legacy", "replacement")
EVIDENCE_KINDS = (
    "openapi", "swagger", "json_schema", "fixtures", "markdown", "manual_example", "unknown",
)
PARSE_STATUSES = ("parsed", "partial", "failed", "unsupported")
FACT_BASES = ("declared", "exemplified")
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


class EvidenceFile(Base):
    """An imported evidence file. Never discarded silently: a file that cannot
    be parsed is still stored, with a report saying why."""

    __tablename__ = "evidence_files"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256"),
        CheckConstraint(_in("side", SIDES), name="side"),
        CheckConstraint(_in("kind", EVIDENCE_KINDS), name="kind"),
        CheckConstraint(_in("parse_status", PARSE_STATUSES), name="parse_status"),
        Index("ix_evidence_files_project_id_created_at", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    side: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100), default="")
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Hash of the bytes as uploaded. The stored content of a fixture file is the
    # redacted form, so the uploaded credentials are not kept.
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    parse_status: Mapped[str] = mapped_column(String(16))
    parse_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Soft delete: out of the current evidence set, kept for historical runs.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiSpec(Base):
    __tablename__ = "api_specs"
    __table_args__ = (CheckConstraint(_in("side", SIDES), name="side"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evidence_file_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_files.id", ondelete="CASCADE")
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    side: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300), default="")
    version: Mapped[str] = mapped_column(String(100), default="")
    spec_format: Mapped[str] = mapped_column(String(32))
    servers: Mapped[list[Any]] = mapped_column(PortableJSON, default=list)
    auth_schemes: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("project_id", "side", "method", "path_template"),
        CheckConstraint(_in("side", SIDES), name="side"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # Null when the endpoint is established by fixtures alone.
    api_spec_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_specs.id", ondelete="SET NULL")
    )
    side: Mapped[str] = mapped_column(String(16))
    method: Mapped[str] = mapped_column(String(16))
    path_template: Mapped[str] = mapped_column(String(500))
    operation_id: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    parameters: Mapped[list[Any]] = mapped_column(PortableJSON, default=list)
    request_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    response_schemas: Mapped[dict[str, Any]] = mapped_column(default=dict)


class EndpointFact(Base):
    """One extracted fact and where it came from. Every fact has a source."""

    __tablename__ = "endpoint_facts"
    __table_args__ = (
        CheckConstraint(_in("basis", FACT_BASES), name="basis"),
        Index("ix_endpoint_facts_endpoint_id_fact_type", "endpoint_id", "fact_type"),
        Index("ix_endpoint_facts_evidence_file_id", "evidence_file_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    fact_type: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(500))
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evidence_file_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_files.id", ondelete="CASCADE")
    )
    # JSON pointer into the specification, or "fixture:<name>" for a fixture.
    locator: Mapped[str] = mapped_column(String(1000))
    basis: Mapped[str] = mapped_column(String(16))


class Fixture(Base):
    """A recorded request/response pair. Immutable after import."""

    __tablename__ = "fixtures"
    __table_args__ = (
        UniqueConstraint("evidence_file_id", "name"),
        CheckConstraint(_in("side", SIDES), name="side"),
        Index("ix_fixtures_project_id_side", "project_id", "side"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    evidence_file_id: Mapped[str] = mapped_column(
        ForeignKey("evidence_files.id", ondelete="CASCADE")
    )
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id", ondelete="SET NULL"))
    side: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(300))
    # On a replacement recording: the legacy fixture it is paired with.
    pair_with_name: Mapped[str | None] = mapped_column(String(300))
    pair_with: Mapped[str | None] = mapped_column(ForeignKey("fixtures.id", ondelete="SET NULL"))
    request: Mapped[dict[str, Any]] = mapped_column(default=dict)
    expected_response: Mapped[dict[str, Any]] = mapped_column(default=dict)
    content_sha256: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[str | None] = mapped_column(String(40))
    normalisation: Mapped[dict[str, Any]] = mapped_column(default=dict)
