from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_scan_context_fetches_independent_sources_concurrently(monkeypatch) -> None:
    import boardman.services.scan_handler as scan_handler

    started: list[str] = []
    release = asyncio.Event()

    async def wait_for_release(name: str) -> str:
        started.append(name)
        await release.wait()
        return f"{name} data"

    async def direction(_client, _owner, _repo):
        return await wait_for_release("direction")

    async def commits(_client, _owner, _repo):
        return await wait_for_release("commits")

    async def issues(_client, _owner, _repo):
        return await wait_for_release("issues")

    async def plaky(_repo_full, _short):
        return await wait_for_release("plaky")

    monkeypatch.setattr(scan_handler, "fetch_direction_md", direction)
    monkeypatch.setattr(scan_handler, "fetch_recent_commits", commits)
    monkeypatch.setattr(scan_handler, "fetch_open_issues", issues)
    monkeypatch.setattr(scan_handler, "fetch_plaky_titles_for_repo", plaky)

    task = asyncio.create_task(scan_handler._fetch_scan_context("o/r", "o", "r", "r"))
    await asyncio.sleep(0.01)
    assert set(started) == {"direction", "commits", "issues", "plaky"}
    release.set()

    assert await task == (
        "direction data",
        "commits data",
        "issues data",
        "plaky data",
    )
