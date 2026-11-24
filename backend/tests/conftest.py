"""Shared fixtures.

Database tests run against a schema created by the real migrations: a fresh
SQLite file per test by default, or the PostgreSQL database named by
TEST_DATABASE_URL (as in CI), which is emptied and migrated for every test.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from revenant.config import Settings
from revenant.db.base import Base
from revenant.db.models import Project
from revenant.db.session import Database, alembic_config, make_engine
from revenant.main import create_app
from revenant.security.redaction import SECRETS

ADMIN_TOKEN = "test-admin-token-0123456789abcdef"
MASTER_KEY = base64.urlsafe_b64encode(b"m" * 32).decode()
OTHER_MASTER_KEY = base64.urlsafe_b64encode(b"n" * 32).decode()


def _reset_postgres(url: str) -> None:
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            Base.metadata.drop_all(connection)
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """An empty database (no tables)."""
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        _reset_postgres(external)
        return external
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_url(database_url: str) -> str:
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    SECRETS.clear()
    yield
    SECRETS.clear()


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "admin_token": ADMIN_TOKEN,
        "secret_key": MASTER_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings(migrated_url: str) -> Settings:
    return make_settings(migrated_url)


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    db = Database(settings.database_url)
    yield db
    db.dispose()


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    with database.session() as active:
        yield active


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, supervised=False)) as test_client:
        yield test_client


@pytest.fixture
def admin() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def project_id(database: Database) -> str:
    """A project row. The projects API arrives in slice 3; until then tests insert directly."""
    with database.session() as active:
        project = Project(
            slug="gravepay", name="GravePay resurrection",
            legacy_name="GravePay", replacement_name="NovaPay",
        )
        active.add(project)
        active.flush()
        return project.id
