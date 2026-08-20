"""The periodic knowledge sweep.

The requirement that makes or breaks it: **a cycle where nothing changed must do almost
nothing**. A sweep that refetches every repo every ten minutes is a crawler wearing a
reconciliation badge, and it would burn the API budget it exists to protect.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, ProjectContext
from boardman.services import repo_knowledge

REPO = "Team-Deepiri/deepiri-boardman"
PUSHED = "2026-08-20T09:00:00Z"


@pytest_asyncio.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _store(factory, repo: str = REPO, revision: str = PUSHED) -> None:
    async with factory() as s:
        s.add(
            ProjectContext(
                repo=repo,
                context_json=json.dumps({"ok": True, "repo": repo}),
                context_source_revision=revision,
                context_fetched_at=datetime.utcnow(),
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_an_unchanged_repo_costs_one_call_and_no_refresh(factory, monkeypatch) -> None:
    calls: list[str] = []
    refreshes: list[str] = []

    async def fake_current(repo: str):
        calls.append(repo)
        return PUSHED, ""

    async def fake_refresh(payload):
        refreshes.append(payload["repo"])
        return {"ok": True}

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    monkeypatch.setattr("boardman.jobs.handlers.boardman_repo_refresh_job", fake_refresh)
    await _store(factory)

    out = await repo_knowledge.sweep_repo_knowledge([REPO], session_factory=factory)

    assert calls == [REPO], "exactly one metadata call"
    assert refreshes == [], "nothing changed, so nothing was refetched"
    assert out["refreshed"] == 0
    assert out["results"][0]["action"] == "unchanged"


@pytest.mark.asyncio
async def test_a_moved_repo_is_refreshed(factory, monkeypatch) -> None:
    refreshes: list[str] = []

    async def fake_current(_repo: str):
        return "2026-08-20T11:11:11Z", ""

    async def fake_refresh(payload):
        refreshes.append(payload["repo"])
        return {"ok": True}

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    monkeypatch.setattr("boardman.jobs.handlers.boardman_repo_refresh_job", fake_refresh)
    await _store(factory)

    out = await repo_knowledge.sweep_repo_knowledge([REPO], session_factory=factory)
    assert refreshes == [REPO]
    assert out["refreshed"] == 1
    assert out["results"][0]["was"] == PUSHED


@pytest.mark.asyncio
async def test_a_repo_with_no_snapshot_is_fetched(factory, monkeypatch) -> None:
    refreshes: list[str] = []

    async def fake_current(_repo: str):
        return PUSHED, ""

    async def fake_refresh(payload):
        refreshes.append(payload["repo"])
        return {"ok": True}

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    monkeypatch.setattr("boardman.jobs.handlers.boardman_repo_refresh_job", fake_refresh)

    out = await repo_knowledge.sweep_repo_knowledge([REPO], session_factory=factory)
    assert refreshes == [REPO]
    assert out["results"][0]["was"] == "(none)"


@pytest.mark.asyncio
async def test_one_broken_repo_does_not_stop_the_others(factory, monkeypatch) -> None:
    async def fake_current(repo: str):
        if "broken" in repo:
            return "", "HTTP 500"
        return "moved", ""

    seen: list[str] = []

    async def fake_refresh(payload):
        seen.append(payload["repo"])
        return {"ok": True}

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    monkeypatch.setattr("boardman.jobs.handlers.boardman_repo_refresh_job", fake_refresh)
    await _store(factory)

    out = await repo_knowledge.sweep_repo_knowledge(
        ["Team-Deepiri/broken", REPO, "Team-Deepiri/other"], session_factory=factory
    )
    assert out["errors"] == 1
    assert sorted(seen) == sorted([REPO, "Team-Deepiri/other"])
    assert out["checked"] == 3


@pytest.mark.asyncio
async def test_a_refresh_that_raises_is_reported_not_swallowed_silently(
    factory, monkeypatch
) -> None:
    async def fake_current(_repo: str):
        return "moved", ""

    async def boom(_payload):
        raise RuntimeError("github exploded")

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    monkeypatch.setattr("boardman.jobs.handlers.boardman_repo_refresh_job", boom)
    await _store(factory)

    out = await repo_knowledge.sweep_repo_knowledge([REPO], session_factory=factory)
    assert out["errors"] == 1
    assert out["refreshed"] == 0


@pytest.mark.asyncio
async def test_an_empty_target_list_is_a_no_op() -> None:
    out = await repo_knowledge.sweep_repo_knowledge([])
    assert out == {"ok": True, "checked": 0, "refreshed": 0, "results": []}


@pytest.mark.asyncio
async def test_concurrency_is_bounded(factory, monkeypatch) -> None:
    """One slow repo must not let the sweep open twenty connections at once."""
    import asyncio

    live = 0
    peak = 0

    async def fake_current(_repo: str):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return PUSHED, ""

    monkeypatch.setattr(repo_knowledge, "current_revision", fake_current)
    repos = [f"Team-Deepiri/r{i}" for i in range(10)]
    for r in repos:
        await _store(factory, repo=r)

    await repo_knowledge.sweep_repo_knowledge(repos, concurrency=3, session_factory=factory)
    assert peak <= 3, f"sweep ran {peak} repos at once with a limit of 3"


def test_sweep_targets_are_full_names_and_capped() -> None:
    targets = repo_knowledge.sweep_targets()
    assert targets, "there should be registered repos to sweep"
    assert all("/" in t for t in targets), "every target must be owner/name"
    assert len(targets) == len(set(targets))
    from boardman.settings import settings

    assert len(targets) <= settings.repo_knowledge_sweep_max_repos


def test_the_worker_starts_the_sweep() -> None:
    import inspect

    from boardman import sqlite_worker

    src = inspect.getsource(sqlite_worker)
    assert "_repo_knowledge_loop" in src
    assert "repo_knowledge_sweep_enabled" in src, "the sweep has to be switchable off"
    assert "knowledge_task.cancel()" in src or "task.cancel()" in src
