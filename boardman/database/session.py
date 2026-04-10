from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.settings import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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