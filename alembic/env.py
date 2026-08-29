import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from boardman.database.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini pins sqlite for zero-config local dev. A real deploy (Postgres) sets
# DATABASE_URL / boardman.settings.database_url, which must win here — otherwise
# `alembic upgrade head` silently migrates the wrong (sqlite) database regardless of
# what the app itself is configured to use.
from boardman.settings import settings  # noqa: E402

if settings.database_url:
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _widen_version_table(connection) -> None:
    """Alembic's version_num column is a hardcoded VARCHAR(32) (alembic/ddl/impl.py).
    This project's revision ids are the migration filenames, several of which are
    longer than 32 chars (e.g. ``008_agent_session_task_draft_json``, 34 chars).
    SQLite never enforces VARCHAR length so this was invisible there; Postgres does,
    and raises StringDataRightTruncationError stamping the very first over-length
    revision. Alembic creates its version table with ``checkfirst=True``, so
    pre-creating it here with a wide column makes it a no-op everywhere else.
    """
    from sqlalchemy import text

    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(255) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
            ")"
        )
    )
    connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))


def do_run_migrations(connection):
    _widen_version_table(connection)
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # Alembic's own transaction wrapping (context.begin_transaction(), via the sync
        # proxy `run_sync` bridges) does not reliably commit through to the underlying
        # asyncpg connection here — verified by hand: every "Running upgrade" line logs
        # and `command.upgrade` returns clean, but the tables never exist afterward
        # without this. SQLite never surfaced it because sqlite3/aiosqlite connections
        # commit implicitly on close in a way asyncpg's do not.
        await connection.commit()

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
