"""Reconstructions, mappings, decisions, gaps, invariants and the behaviour inventory.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
CONFIDENCE = "confidence IN ('observed', 'inferred')"


def _id() -> sa.Column[str]:
    return sa.Column("id", sa.String(36), nullable=False)


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], ondelete=ondelete, name=f"fk_{table}_{column}_{target}"
    )


def _owned(table: str) -> list[sa.schema.SchemaItem]:
    """Columns and keys shared by every row that belongs to a reconstruction."""
    return [
        _id(),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("reconstruction_id", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        _fk(table, "project_id", "projects", "CASCADE"),
        _fk(table, "reconstruction_id", "reconstructions", "CASCADE"),
    ]


def upgrade() -> None:
    op.create_table(
        "reconstructions",
        _id(),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("engine_version", sa.String(16), nullable=False),
        sa.Column("summary", JSON, nullable=False),
        sa.Column("report", JSON, nullable=False),
        sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_file_ids", JSON, nullable=False),
        sa.Column("inventory_size", sa.Integer(), nullable=False),
        sa.Column("inventory_fingerprint", sa.String(64), nullable=False),
        sa.Column("previous_reconstruction_id", sa.String(36), nullable=True),
        sa.Column("inventory_diff", JSON, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reconstructions"),
        _fk("reconstructions", "project_id", "projects", "CASCADE"),
        _fk("reconstructions", "previous_reconstruction_id", "reconstructions", "SET NULL"),
        sa.UniqueConstraint("project_id", "sequence", name="uq_reconstructions_project_id"),
    )

    op.create_table(
        "mappings",
        *_owned("mappings"),
        sa.Column("key", sa.String(700), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("legacy_endpoint_id", sa.String(36), nullable=True),
        sa.Column("replacement_endpoint_id", sa.String(36), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("legacy_ref", sa.String(700), nullable=False),
        sa.Column("replacement_ref", sa.String(700), nullable=False),
        sa.Column("transformation", JSON, nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("rule_name", sa.String(64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_refs", JSON, nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _fk("mappings", "legacy_endpoint_id", "endpoints", "SET NULL"),
        _fk("mappings", "replacement_endpoint_id", "endpoints", "SET NULL"),
        _fk("mappings", "supersedes_id", "mappings", "SET NULL"),
        sa.CheckConstraint(
            "category IN ('endpoint', 'request_field', 'response_field', 'query', 'header', "
            "'auth', 'pagination', 'status_code', 'error', 'enum', 'type', 'effect')",
            name="ck_mappings_category",
        ),
        sa.CheckConstraint(CONFIDENCE, name="ck_mappings_confidence"),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_mappings_status",
        ),
        sa.CheckConstraint("origin IN ('engine', 'human')", name="ck_mappings_origin"),
    )
    op.create_index("ix_mappings_project_id_status", "mappings", ["project_id", "status"])
    op.create_index("ix_mappings_reconstruction_id", "mappings", ["reconstruction_id"])

    op.create_table(
        "mapping_decisions",
        _id(),
        sa.Column("mapping_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mapping_decisions"),
        _fk("mapping_decisions", "mapping_id", "mappings", "CASCADE"),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'edited')", name="ck_mapping_decisions_decision"
        ),
    )
    op.create_index("ix_mapping_decisions_mapping_id", "mapping_decisions", ["mapping_id"])

    op.create_table(
        "gaps",
        *_owned("gaps"),
        sa.Column("key", sa.String(700), nullable=False),
        sa.Column("endpoint_id", sa.String(36), nullable=True),
        sa.Column("endpoint_ref", sa.String(600), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence_refs", JSON, nullable=False),
        sa.Column("missing", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("behaviour_key", sa.String(700), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=False),
        _fk("gaps", "endpoint_id", "endpoints", "SET NULL"),
        sa.UniqueConstraint("reconstruction_id", "key", name="uq_gaps_reconstruction_id"),
        sa.CheckConstraint("severity IN ('blocking', 'advisory')", name="ck_gaps_severity"),
        sa.CheckConstraint(
            "resolution IN ('open', 'accepted_risk', 'resolved_by_evidence')",
            name="ck_gaps_resolution",
        ),
    )

    op.create_table(
        "invariants",
        *_owned("invariants"),
        sa.Column("key", sa.String(700), nullable=False),
        sa.Column("endpoint_id", sa.String(36), nullable=True),
        sa.Column("endpoint_ref", sa.String(600), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("definition", JSON, nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("evidence_refs", JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        _fk("invariants", "endpoint_id", "endpoints", "SET NULL"),
        sa.UniqueConstraint("reconstruction_id", "key", name="uq_invariants_reconstruction_id"),
        sa.CheckConstraint(
            "kind IN ('echo', 'schema', 'pagination', 'error')", name="ck_invariants_kind"
        ),
        sa.CheckConstraint(CONFIDENCE, name="ck_invariants_confidence"),
        sa.CheckConstraint(
            "status IN ('active', 'proposed', 'rejected')", name="ck_invariants_status"
        ),
    )

    op.create_table(
        "behaviours",
        *_owned("behaviours"),
        sa.Column("endpoint_id", sa.String(36), nullable=True),
        sa.Column("endpoint_ref", sa.String(600), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(700), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("evidence_refs", JSON, nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_note", sa.Text(), nullable=False),
        sa.Column("unsupported_reason", sa.Text(), nullable=True),
        _fk("behaviours", "endpoint_id", "endpoints", "SET NULL"),
        sa.UniqueConstraint("reconstruction_id", "key", name="uq_behaviours_reconstruction_id"),
        sa.CheckConstraint(
            "kind IN ('response', 'auth', 'pagination_traversal', 'pagination_termination', "
            "'gap', 'unsupported')",
            name="ck_behaviours_kind",
        ),
        sa.CheckConstraint(
            "scope IN ('in_scope', 'out_of_scope')", name="ck_behaviours_scope"
        ),
    )
    op.create_index(
        "ix_behaviours_project_id_reconstruction_id", "behaviours",
        ["project_id", "reconstruction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_behaviours_project_id_reconstruction_id", table_name="behaviours")
    op.drop_table("behaviours")
    op.drop_table("invariants")
    op.drop_table("gaps")
    op.drop_index("ix_mapping_decisions_mapping_id", table_name="mapping_decisions")
    op.drop_table("mapping_decisions")
    op.drop_index("ix_mappings_reconstruction_id", table_name="mappings")
    op.drop_index("ix_mappings_project_id_status", table_name="mappings")
    op.drop_table("mappings")
    op.drop_table("reconstructions")
