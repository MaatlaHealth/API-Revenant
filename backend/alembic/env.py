"""Alembic environment.

The URL comes from ``config.attributes["url"]`` when the application or the
tests drive migrations, and otherwise from the DATABASE_URL setting when the
``alembic`` command line is used.
"""

from __future__ import annotations

from alembic import context

from revenant.config import Settings
from revenant.db import models  # noqa: F401 - registers the tables on Base.metadata
from revenant.db.base import Base
from revenant.db.session import make_engine, normalise_database_url

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url")
    if url:
        return str(url)
    return normalise_database_url(Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
