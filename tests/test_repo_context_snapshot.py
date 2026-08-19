from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.repo_context import (
    load_planning_snapshot,
    save_planning_snapshot,
    snapshot_prompt_block,
)
from boardman.database.models import Base, ProjectContext


@pytest.mark.asyncio
async def test_snapshot_round_trip_is_bounded_and_reusable(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_repo_context_cache_ttl_seconds", 900.0)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload = {
        "ok": True,
        "repo": "deepiri/boardman",
        "structure": {
            "language": "Python",
            "default_branch": "main",
            "top_level_dirs": ["boardman", "tests"],
        },
        "DIRECTION_md": "Ship reliable sync automation.",
        "readme_md": "A useful README.",
    }

    try:
        async with factory() as session:
            await save_planning_snapshot(
                session, "deepiri/boardman", payload, source_revision="2026-08-18T00:00:00Z"
            )
            await session.commit()
        async with factory() as session:
            raw, state = await load_planning_snapshot(session, "deepiri/boardman")
            assert state == "fresh"
            assert json.loads(raw or "{}")["repo"] == "deepiri/boardman"
            prompt = snapshot_prompt_block(raw)
            assert "Ship reliable sync automation." in prompt
            assert "A useful README." in prompt
            assert len(prompt) <= 6500
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_snapshot_is_only_returned_in_explicit_degraded_mode(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "agent_repo_context_cache_ttl_seconds", 1.0)
    monkeypatch.setattr(bs.settings, "agent_repo_context_stale_if_error_seconds", 60.0)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            row = ProjectContext(
                repo="deepiri/boardman",
                context_json=json.dumps({"ok": True, "repo": "deepiri/boardman"}),
                context_fetched_at=datetime.utcnow() - timedelta(seconds=10),
            )
            session.add(row)
            await session.commit()
        async with factory() as session:
            raw, state = await load_planning_snapshot(session, "deepiri/boardman")
            assert raw is None
            assert state == "miss"
            raw, state = await load_planning_snapshot(
                session, "deepiri/boardman", allow_stale=True
            )
            assert raw is not None
            assert state == "stale"
            assert json.loads(raw)["cache"]["state"] == "stale-fallback"
    finally:
        await engine.dispose()
