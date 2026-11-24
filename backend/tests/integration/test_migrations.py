"""Migrations (requirement 12.1). Runs on SQLite locally and on PostgreSQL in CI."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from revenant.db.base import Base
from revenant.db.models import OutboundCredential, Project
from revenant.db.session import Database, alembic_config, make_engine, normalise_database_url

TABLES = {"projects", "inbound_keys", "outbound_credentials", "audit_records"}


def test_upgrade_creates_the_tables(migrated_url: str) -> None:
    engine = make_engine(migrated_url)
    try:
        assert TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_models_and_migrations_describe_the_same_schema(migrated_url: str) -> None:
    """If a model changes without a migration, or the reverse, this fails."""
    engine = make_engine(migrated_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "render_as_batch": True}
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert differences == []


def test_downgrade_removes_everything_and_upgrade_works_again(migrated_url: str) -> None:
    config = alembic_config(migrated_url)
    command.downgrade(config, "base")
    engine = make_engine(migrated_url)
    try:
        assert not TABLES & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    database = Database(migrated_url)
    try:
        assert database.schema_is_current()
    finally:
        database.dispose()


def test_upgrade_is_idempotent(migrated_url: str) -> None:
    command.upgrade(alembic_config(migrated_url), "head")
    database = Database(migrated_url)
    try:
        assert database.ping() and database.schema_is_current()
    finally:
        database.dispose()


def test_schema_is_not_current_before_migrating(database_url: str) -> None:
    database = Database(database_url)
    try:
        assert database.ping() is True
        assert database.schema_is_current() is False
    finally:
        database.dispose()


def test_foreign_keys_are_enforced(database: Database) -> None:
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(OutboundCredential(
                project_id="does-not-exist", name="token", version=1,
                ciphertext=b"x", key_id="k", status="active",
            ))


def test_check_constraint_rejects_unknown_status(database: Database) -> None:
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(Project(slug="p", name="P", legacy_name="A", replacement_name="B",
                                status="not-a-status"))


def test_only_one_active_credential_version_per_name(database: Database, project_id: str) -> None:
    def row(version: int) -> OutboundCredential:
        return OutboundCredential(project_id=project_id, name="token", version=version,
                                  ciphertext=b"x", key_id="k", status="active")

    with database.session() as session:
        session.add(row(1))
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(row(2))


def test_deleting_a_project_removes_its_credentials(database: Database, project_id: str) -> None:
    with database.session() as session:
        session.add(OutboundCredential(project_id=project_id, name="token", version=1,
                                       ciphertext=b"x", key_id="k", status="active"))
    with database.session() as session:
        session.delete(session.get(Project, project_id))
    with database.session() as session:
        assert session.execute(text("SELECT count(*) FROM outbound_credentials")).scalar() == 0


def test_failed_unit_of_work_is_rolled_back(database: Database) -> None:
    with pytest.raises(RuntimeError):
        with database.session() as session:
            session.add(Project(slug="temp", name="T", legacy_name="A", replacement_name="B"))
            session.flush()
            raise RuntimeError("abort")
    with database.session() as session:
        assert session.execute(text("SELECT count(*) FROM projects")).scalar() == 0


@pytest.mark.parametrize("given, expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("sqlite:///./revenant.db", "sqlite:///./revenant.db"),
])
def test_platform_database_urls_are_normalised(given: str, expected: str) -> None:
    assert normalise_database_url(given) == expected
