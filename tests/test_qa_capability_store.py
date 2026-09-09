"""qa_capability_profiles DB store: upsert/read, and the sync in-memory cache that
boardman/assignment/config.py's live qa_tier resolution actually reads from."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.services import qa_capability_store as store


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_cache():
    store.clear_capability_cache()
    yield
    store.clear_capability_cache()


@pytest.mark.asyncio
async def test_upsert_then_insert_then_update(db_session) -> None:
    await store.upsert_profile(
        db_session, github_login="Alice", qa_tier=2, repos_discovered=3, repos_mined=2
    )
    await db_session.commit()
    profiles = await store.all_profiles(db_session)
    assert profiles["alice"]["qa_tier"] == 2
    assert profiles["alice"]["repos_mined"] == 2

    # Re-upsert the SAME login (case-insensitive) must update, not duplicate.
    await store.upsert_profile(
        db_session, github_login="alice", qa_tier=3, repos_discovered=5, repos_mined=4
    )
    await db_session.commit()
    profiles = await store.all_profiles(db_session)
    assert len(profiles) == 1
    assert profiles["alice"]["qa_tier"] == 3
    assert profiles["alice"]["repos_mined"] == 4


@pytest.mark.asyncio
async def test_upsert_empty_login_is_a_noop(db_session) -> None:
    await store.upsert_profile(db_session, github_login="  ", qa_tier=3)
    await db_session.commit()
    assert await store.all_profiles(db_session) == {}


@pytest.mark.asyncio
async def test_refresh_capability_cache_populates_from_db(db_session, monkeypatch) -> None:
    await store.upsert_profile(db_session, github_login="bob", qa_tier=1)
    await store.upsert_profile(db_session, github_login="carol", qa_tier=None, reason="no evidence")
    await db_session.commit()

    class _FakeSessionCtx:
        async def __aenter__(self) -> AsyncSession:
            return db_session

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(store, "async_session", lambda: _FakeSessionCtx())

    assert store.cached_capability_tiers() == {}
    await store.refresh_capability_cache()
    tiers = store.cached_capability_tiers()
    assert tiers == {"bob": 1}  # carol's None qa_tier is excluded, not coerced to 0/1


@pytest.mark.asyncio
async def test_refresh_capability_cache_failure_keeps_previous_cache(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "async_session", _boom)
    store._capability_cache = {"preexisting": 2}
    await store.refresh_capability_cache()
    assert store.cached_capability_tiers() == {"preexisting": 2}
