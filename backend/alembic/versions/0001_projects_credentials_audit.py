"""Projects, credential tables and the audit log.

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("legacy_name", sa.String(200), nullable=False),
        sa.Column("replacement_name", sa.String(200), nullable=False),
        sa.Column("replacement_base_url", sa.String(2000), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("settings", JSON, nullable=False),
        sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
        sa.CheckConstraint(
            "status IN ('draft', 'evidence', 'reconstructed', 'adapter_ready', "
            "'verified', 'running')",
            name="ck_projects_status",
        ),
    )

    op.create_table(
        "inbound_keys",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("verifier", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_keys"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_inbound_keys_project_id_projects",
        ),
        sa.UniqueConstraint("project_id", "label", name="uq_inbound_keys_project_id"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_inbound_keys_status"),
    )
    op.create_index(
        "ix_inbound_keys_project_id_status", "inbound_keys", ["project_id", "status"]
    )

    op.create_table(
        "outbound_credentials",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbound_credentials"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_outbound_credentials_project_id_projects",
        ),
        sa.UniqueConstraint(
            "project_id", "name", "version", name="uq_outbound_credentials_project_id"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'previous', 'retired')", name="ck_outbound_credentials_status"
        ),
    )
    op.create_index(
        "uq_outbound_credentials_one_active",
        "outbound_credentials",
        ["project_id", "name"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "audit_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("metadata", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_records"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_audit_records_project_id_projects",
        ),
    )
    op.create_index(
        "ix_audit_records_project_id_created_at", "audit_records", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_records_project_id_created_at", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_index("uq_outbound_credentials_one_active", table_name="outbound_credentials")
    op.drop_table("outbound_credentials")
    op.drop_index("ix_inbound_keys_project_id_status", table_name="inbound_keys")
    op.drop_table("inbound_keys")
    op.drop_table("projects")
