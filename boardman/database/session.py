from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.settings import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
        await session.commit()


async def init_db() -> None:
    from boardman.database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)