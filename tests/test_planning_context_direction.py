from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, ProjectContext, ScanRun
from boardman.planning.huddle.context_direction import DirectionPlanningContext


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_direction_context_uses_fresh_cache() -> None:
    _, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            ProjectContext(
                repo="Team-Deepiri/deepiri-platform",
                summary="Ship auth refactor and stabilize API gateway this sprint.",
                last_scanned=datetime.utcnow(),
            )
        )
        await session.commit()

    ctx = DirectionPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
        direction_fetcher=_never_called_fetcher,
    )
    md = await ctx._context_markdown_async("qa")
    assert "## Repo Direction" in md
    assert "### DIRECTION summaries" in md
    assert "Team-Deepiri/deepiri-platform" in md
    assert "(cache)" in md
    assert "auth refactor" in md


@pytest.mark.asyncio
async def test_direction_context_fetches_github_when_cache_stale(monkeypatch) -> None:
    _, factory = await _memory_session_factory()
    stale = datetime.utcnow() - timedelta(hours=48)
    async with factory() as session:
        session.add(
            ProjectContext(
                repo="Team-Deepiri/deepiri-platform",
                summary="Old cached summary that should not win.",
                last_scanned=stale,
            )
        )
        await session.commit()

    async def fake_fetch(repo_full: str) -> str:
        return "# Direction\n\nFocus on reliability and QA automation."

    monkeypatch.setattr("boardman.settings.settings.github_pat", "test-token")

    ctx = DirectionPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
        direction_fetcher=fake_fetch,
    )
    md = await ctx._context_markdown_async("qa")
    assert "(github)" in md
    assert "reliability and QA automation" in md


@pytest.mark.asyncio
async def test_direction_context_includes_recent_scans() -> None:
    _, factory = await _memory_session_factory()
    tasks = json.dumps([{"title": "Task A"}, {"title": "Task B"}])
    async with factory() as session:
        session.add(
            ScanRun(
                github_repo="Team-Deepiri/deepiri-platform",
                tasks_proposed=tasks,
                tasks_created=1,
                created_at=datetime.utcnow(),
            )
        )
        await session.commit()

    ctx = DirectionPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
        direction_fetcher=_never_called_fetcher,
    )
    md = await ctx._context_markdown_async("qa")
    assert "### Recent AI scans" in md
    assert "proposed 2 tasks" in md
    assert "created 1" in md


@pytest.mark.asyncio
async def test_direction_context_empty() -> None:
    _, factory = await _memory_session_factory()
    ctx = DirectionPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
        direction_fetcher=_never_called_fetcher,
    )
    md = await ctx._context_markdown_async("qa")
    assert "No repo direction or scan history available" in md


async def _never_called_fetcher(repo_full: str) -> str:
    raise AssertionError(f"fetch should not run for {repo_full}")
