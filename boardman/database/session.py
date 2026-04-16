from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.settings import settings

_engine_kwargs = {"echo": False}
if settings.database_url.startswith("sqlite"):
    # Wait longer on write contention instead of failing fast with "database is locked".
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            # WAL improves read/write concurrency for API + worker access.
            cur.execute("PRAGMA journal_mode=WAL")
            # Keep writer waiting a bit before returning "database is locked".
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    from sqlalchemy import text

    from boardman.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if settings.database_url.startswith("sqlite"):
            def _add_task_draft_column(sync_conn):
                r = sync_conn.execute(text("PRAGMA table_info(agent_sessions)"))
                cols = [row[1] for row in r.fetchall()]
                if "task_draft_json" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE agent_sessions ADD COLUMN task_draft_json TEXT")
                    )

            await conn.run_sync(_add_task_draft_column)