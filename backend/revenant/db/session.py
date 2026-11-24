"""Engine and session management.

Database access is synchronous and runs off the event loop: FastAPI executes
plain ``def`` endpoints and dependencies in its thread pool.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[2]
SQLITE_BUSY_TIMEOUT_MS = 5000


def normalise_database_url(url: str) -> str:
    """Select the psycopg (v3) driver for PostgreSQL URLs from hosting platforms."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalise_database_url(url)
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def alembic_config(url: str) -> Config:
    """Alembic configuration bound to ``url``, independent of the working directory."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # Passed as an attribute, not an ini option: URLs may contain '%'.
    config.attributes["url"] = normalise_database_url(url)
    return config


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str) -> None:
        self.url = normalise_database_url(url)
        self.engine = make_engine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A unit of work: commit on success, roll back on any error."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    def schema_is_current(self) -> bool:
        """True when the database is at the newest migration."""
        head = ScriptDirectory.from_config(alembic_config(self.url)).get_current_head()
        try:
            with self.engine.connect() as connection:
                current = MigrationContext.configure(connection).get_current_revision()
        except SQLAlchemyError:
            return False
        return current is not None and current == head

    def dispose(self) -> None:
        self.engine.dispose()
