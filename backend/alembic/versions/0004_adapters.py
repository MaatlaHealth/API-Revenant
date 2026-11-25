"""Adapter versions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "adapter_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reconstruction_id", sa.String(36), nullable=False),
        sa.Column("document", JSON, nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("mapping_ids", JSON, nullable=False),
        sa.Column("report", JSON, nullable=False),
        sa.Column("compiled_by", sa.String(64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_adapter_versions"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_adapter_versions_project_id_projects",
        ),
        sa.ForeignKeyConstraint(
            ["reconstruction_id"], ["reconstructions.id"], ondelete="CASCADE",
            name="fk_adapter_versions_reconstruction_id_reconstructions",
        ),
        sa.UniqueConstraint("project_id", "version", name="uq_adapter_versions_project_id"),
    )


def downgrade() -> None:
    op.drop_table("adapter_versions")
