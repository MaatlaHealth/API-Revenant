"""Runtime instances, legacy hosts, runtime events and the instance lease.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _project(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                   name=f"fk_{table}_project_id_projects")


def _adapter(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["adapter_version_id"], ["adapter_versions.id"], ondelete="SET NULL",
        name=f"fk_{table}_adapter_version_id_adapter_versions")


def upgrade() -> None:
    op.create_table(
        "runtime_instances",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("adapter_version_id", sa.String(36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_loaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config", JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_instances"),
        _project("runtime_instances"),
        _adapter("runtime_instances"),
        sa.UniqueConstraint("project_id", name="uq_runtime_instances_project_id"),
        sa.CheckConstraint("desired_state IN ('running', 'stopped')",
                           name="ck_runtime_instances_desired_state"),
        sa.CheckConstraint("status IN ('stopped', 'starting', 'running', 'failed')",
                           name="ck_runtime_instances_status"),
        sa.CheckConstraint("desired_state != 'running' OR adapter_version_id IS NOT NULL",
                           name="ck_runtime_instances_pinned"),
    )
    op.create_table(
        "runtime_hosts",
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("host", name="pk_runtime_hosts"),
        _project("runtime_hosts"),
    )
    op.create_table(
        "runtime_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("entry", sa.String(8), nullable=True),
        sa.Column("original_host", sa.String(300), nullable=True),
        sa.Column("method", sa.String(16), nullable=True),
        sa.Column("legacy_path", sa.String(2000), nullable=True),
        sa.Column("legacy_query", sa.Text(), nullable=True),
        sa.Column("query_credential_removed", sa.Boolean(), nullable=False),
        sa.Column("upstream_path", sa.String(2000), nullable=True),
        sa.Column("route_id", sa.String(100), nullable=True),
        sa.Column("legacy_status", sa.Integer(), nullable=True),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("upstream_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("trace", JSON, nullable=False),
        sa.Column("adapter_version_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_events"),
        _project("runtime_events"),
        _adapter("runtime_events"),
        sa.UniqueConstraint("request_id", name="uq_runtime_events_request_id"),
        sa.CheckConstraint("kind IN ('request', 'lifecycle', 'error', 'trace_loss')",
                           name="ck_runtime_events_kind"),
    )
    op.create_index("ix_runtime_events_project_id_created_at", "runtime_events",
                    ["project_id", "created_at"])
    lease = op.create_table(
        "instance_lease",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("holder_id", sa.String(64), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("parent_pid", sa.Integer(), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("beat", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released", sa.Boolean(), nullable=False),
        sa.Column("handover_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handover_requested_by", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_instance_lease"),
        sa.CheckConstraint("id = 1", name="ck_instance_lease_single_row"),
    )
    # The one row. It starts released, so the first process to start acquires it.
    op.bulk_insert(lease, [{"id": 1, "epoch": 0, "beat": 0, "released": True}])


def downgrade() -> None:
    op.drop_table("instance_lease")
    op.drop_index("ix_runtime_events_project_id_created_at", table_name="runtime_events")
    op.drop_table("runtime_events")
    op.drop_table("runtime_hosts")
    op.drop_table("runtime_instances")
