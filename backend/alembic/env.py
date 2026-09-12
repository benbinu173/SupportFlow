"""Alembic environment.

Run through `alembic` (see the Makefile targets), never imported by the app.

Two deliberate choices live here:

**The engine is synchronous.** The application is async end to end, but migrations
are a one-shot batch job where latency is irrelevant, and psycopg 3 speaks both
protocols. Going sync keeps the Windows selector-loop workaround in
app/core/event_loop.py confined to the app, instead of every migration command
needing it too.

**The URL comes from application settings, not from alembic.ini.** The DSN carries a
password, and alembic.ini is committed. Reading `settings.sqlalchemy_dsn` also means
the app and its migrations cannot drift onto different drivers.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base

config = context.config

# Replaces the logging config the app installs, with the one from alembic.ini. Only
# applies when Alembic is driving; importing this module under pytest is not a thing.
#
# disable_existing_loggers=False because the migration tests call Alembic in-process,
# and the default would switch off pytest's own loggers partway through the session.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The metadata autogenerate diffs the database against. Populated only because
# `app.models` imports every model module — see that package's docstring.
target_metadata = Base.metadata

settings = get_settings()

# `config.attributes` is Alembic's supported channel for a programmatic caller to
# hand env.py a value it cannot get from the environment. The migration tests use it
# to point a run at a scratch database. Unset in every other case, where the URL
# comes from application settings — never from alembic.ini, which is committed.
DATABASE_URL: str = config.attributes.get("db_url") or settings.sqlalchemy_dsn


def _configure(connection: Connection | None, url: str | None) -> None:
    """Shared context configuration for both offline and online modes.

    `compare_type` and `compare_server_default` are on so that the drift check has
    teeth: without them, autogenerate reports "no changes" while a column's type or
    default has silently diverged from the models.

    There is no `include_object` filter. Autogenerate diffs tables, columns, indexes,
    constraints, and sequences — it does not diff types or functions, which is all
    that pgvector and pg_trgm install. So there is nothing for a filter to exclude,
    and the extensions would have to start owning a *table* before one was needed.
    """
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Postgres has transactional DDL, so DDL and the version bump that records it
        # commit together. A migration that fails halfway leaves no partial schema
        # behind for the next run to trip over.
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    Used via `alembic upgrade head --sql` to review the DDL a migration will issue,
    or to hand a DBA a script. No engine is created and nothing connects.
    """
    _configure(
        None,
        DATABASE_URL,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations.

    `poolclass=NullPool` because this process runs one batch and exits: a pool would
    only hold connections open past the last migration, which is exactly the state
    that makes `DROP DATABASE` fail with "being accessed by other users".
    """
    engine: Engine = create_engine(DATABASE_URL, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            _configure(connection, None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
