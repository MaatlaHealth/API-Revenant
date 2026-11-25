"""Evidence files, specifications, endpoints, facts and fixtures.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
SIDE = "side IN ('legacy', 'replacement')"


def upgrade() -> None:
    op.create_table(
        "evidence_files",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("parse_status", sa.String(16), nullable=False),
        sa.Column("parse_report", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_files"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_evidence_files_project_id_projects",
        ),
        sa.UniqueConstraint("project_id", "sha256", name="uq_evidence_files_project_id"),
        sa.CheckConstraint(SIDE, name="ck_evidence_files_side"),
        sa.CheckConstraint(
            "kind IN ('openapi', 'swagger', 'json_schema', 'fixtures', 'markdown', "
            "'manual_example', 'unknown')",
            name="ck_evidence_files_kind",
        ),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'partial', 'failed', 'unsupported')",
            name="ck_evidence_files_parse_status",
        ),
    )
    op.create_index(
        "ix_evidence_files_project_id_created_at", "evidence_files", ["project_id", "created_at"]
    )

    op.create_table(
        "api_specs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("evidence_file_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("spec_format", sa.String(32), nullable=False),
        sa.Column("servers", JSON, nullable=False),
        sa.Column("auth_schemes", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_api_specs"),
        sa.ForeignKeyConstraint(
            ["evidence_file_id"], ["evidence_files.id"], ondelete="CASCADE",
            name="fk_api_specs_evidence_file_id_evidence_files",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_api_specs_project_id_projects",
        ),
        sa.CheckConstraint(SIDE, name="ck_api_specs_side"),
    )

    op.create_table(
        "endpoints",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("api_spec_id", sa.String(36), nullable=True),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("path_template", sa.String(500), nullable=False),
        sa.Column("operation_id", sa.String(200), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("parameters", JSON, nullable=False),
        sa.Column("request_schema", JSON, nullable=False),
        sa.Column("response_schemas", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_endpoints"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_endpoints_project_id_projects",
        ),
        sa.ForeignKeyConstraint(
            ["api_spec_id"], ["api_specs.id"], ondelete="SET NULL",
            name="fk_endpoints_api_spec_id_api_specs",
        ),
        sa.UniqueConstraint(
            "project_id", "side", "method", "path_template", name="uq_endpoints_project_id"
        ),
        sa.CheckConstraint(SIDE, name="ck_endpoints_side"),
    )

    op.create_table(
        "endpoint_facts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("endpoint_id", sa.String(36), nullable=False),
        sa.Column("fact_type", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("value", JSON, nullable=False),
        sa.Column("evidence_file_id", sa.String(36), nullable=False),
        sa.Column("locator", sa.String(1000), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_endpoint_facts"),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"], ondelete="CASCADE",
            name="fk_endpoint_facts_endpoint_id_endpoints",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_id"], ["evidence_files.id"], ondelete="CASCADE",
            name="fk_endpoint_facts_evidence_file_id_evidence_files",
        ),
        sa.CheckConstraint(
            "basis IN ('declared', 'exemplified')", name="ck_endpoint_facts_basis"
        ),
    )
    op.create_index(
        "ix_endpoint_facts_endpoint_id_fact_type", "endpoint_facts", ["endpoint_id", "fact_type"]
    )
    op.create_index(
        "ix_endpoint_facts_evidence_file_id", "endpoint_facts", ["evidence_file_id"]
    )

    op.create_table(
        "fixtures",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("evidence_file_id", sa.String(36), nullable=False),
        sa.Column("endpoint_id", sa.String(36), nullable=True),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("pair_with_name", sa.String(300), nullable=True),
        sa.Column("pair_with", sa.String(36), nullable=True),
        sa.Column("request", JSON, nullable=False),
        sa.Column("expected_response", JSON, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=True),
        sa.Column("normalisation", JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_fixtures"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE",
            name="fk_fixtures_project_id_projects",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_id"], ["evidence_files.id"], ondelete="CASCADE",
            name="fk_fixtures_evidence_file_id_evidence_files",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"], ondelete="SET NULL",
            name="fk_fixtures_endpoint_id_endpoints",
        ),
        sa.ForeignKeyConstraint(
            ["pair_with"], ["fixtures.id"], ondelete="SET NULL",
            name="fk_fixtures_pair_with_fixtures",
        ),
        sa.UniqueConstraint("evidence_file_id", "name", name="uq_fixtures_evidence_file_id"),
        sa.CheckConstraint(SIDE, name="ck_fixtures_side"),
    )
    op.create_index("ix_fixtures_project_id_side", "fixtures", ["project_id", "side"])


def downgrade() -> None:
    op.drop_index("ix_fixtures_project_id_side", table_name="fixtures")
    op.drop_table("fixtures")
    op.drop_index("ix_endpoint_facts_evidence_file_id", table_name="endpoint_facts")
    op.drop_index("ix_endpoint_facts_endpoint_id_fact_type", table_name="endpoint_facts")
    op.drop_table("endpoint_facts")
    op.drop_table("endpoints")
    op.drop_table("api_specs")
    op.drop_index("ix_evidence_files_project_id_created_at", table_name="evidence_files")
    op.drop_table("evidence_files")
