"""Creating tasks must not hold the reply hostage to Plaky's write speed.

Five tasks take tens of seconds on the board. Boardman should answer with what it
decided and why in a few seconds, and let the cards land behind the reply. The receipt
it returns therefore describes what is BEING created — claiming "created" before the
writes finish would be the same confident falsehood this codebase exists to avoid.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import BackgroundJob, Base
from boardman.jobs.deferred import wait_for_deferred


@pytest_asyncio.fixture()
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("boardman.broker.job_queue.async_session", factory)
    yield factory
    await wait_for_deferred()
    await engine.dispose()


@pytest.mark.asyncio
async def test_deferred_create_returns_before_the_writes_and_never_says_created(
    db, monkeypatch
) -> None:
    from boardman.agent.tools import plaky_tools as pt

    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[dict[str, Any]] = []

    async def slow_handler(payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await release.wait()  # stands in for Plaky taking its time
        ran.append(payload)
        return {"ok": True, "created_count": len(payload.get("tasks") or [])}

    monkeypatch.setitem(
        __import__("boardman.jobs.handlers", fromlist=["JOB_HANDLERS"]).JOB_HANDLERS,
        "plaky_create_tasks_job",
        slow_handler,
    )

    rows = [{"title": "First task"}, {"title": "Second task", "priority": "High"}]
    raw = await pt._plaky_create_tasks_deferred(json.dumps(rows), board_id="B1", group_id="G1")
    out = json.loads(raw)

    # Returned while the write is still blocked -> the reply did not wait.
    assert out["ok"] is True and out["deferred"] is True
    assert out["queued_count"] == 2 and out["job_id"]
    assert not ran, "the tool returned only after the writes finished"
    assert "First task" in out["receipt_markdown"]
    note = out["note"].lower()
    assert "being written" in note or "landing" in note
    assert "never" in note and "'created'" in note  # must not claim a finished write

    await asyncio.wait_for(started.wait(), timeout=5)
    release.set()
    await wait_for_deferred()
    assert ran and ran[0]["board_id"] == "B1" and ran[0]["group_id"] == "G1"


@pytest.mark.asyncio
async def test_the_outcome_is_recorded_so_a_follow_up_reads_truth(db, monkeypatch) -> None:
    from boardman.agent.tools import plaky_tools as pt
    from boardman.broker.job_queue import get_job_queue, reset_job_queue_for_tests

    reset_job_queue_for_tests()

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "created_count": 1, "failed_count": 0}

    monkeypatch.setitem(
        __import__("boardman.jobs.handlers", fromlist=["JOB_HANDLERS"]).JOB_HANDLERS,
        "plaky_create_tasks_job",
        handler,
    )
    out = json.loads(await pt._plaky_create_tasks_deferred(json.dumps([{"title": "T"}])))
    job_id = out["job_id"]
    await wait_for_deferred()
    for _ in range(50):
        row = await get_job_queue().fetch_public_job(job_id)
        if row and row.get("status") == "complete":
            assert row["result"]["created_count"] == 1
            return
        await asyncio.sleep(0.02)
    raise AssertionError("job never reached a recorded outcome")


@pytest.mark.asyncio
async def test_a_failing_write_is_recorded_not_swallowed(db, monkeypatch) -> None:
    from boardman.agent.tools import plaky_tools as pt
    from boardman.broker.job_queue import get_job_queue

    async def boom(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("plaky exploded")

    monkeypatch.setitem(
        __import__("boardman.jobs.handlers", fromlist=["JOB_HANDLERS"]).JOB_HANDLERS,
        "plaky_create_tasks_job",
        boom,
    )
    out = json.loads(await pt._plaky_create_tasks_deferred(json.dumps([{"title": "T"}])))
    await wait_for_deferred()
    for _ in range(50):
        row = await get_job_queue().fetch_public_job(out["job_id"])
        if row and row.get("status") == "incomplete":
            assert "plaky exploded" in json.dumps(row.get("result") or {})
            return
        await asyncio.sleep(0.02)
    raise AssertionError("a failed background write left no record")


@pytest.mark.asyncio
async def test_only_one_runner_executes_a_job(db) -> None:
    """The standalone worker and the in-process runner must not both write the rows."""
    from boardman.broker.job_queue import claim_job_by_id, get_job_queue

    job = await get_job_queue().enqueue_job("plaky_create_tasks_job", {"tasks": [{"title": "x"}]})
    first = await claim_job_by_id(job.job_id)
    second = await claim_job_by_id(job.job_id)
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_bad_rows_are_refused_up_front(db) -> None:
    from boardman.agent.tools import plaky_tools as pt

    assert json.loads(await pt._plaky_create_tasks_deferred("not json"))["ok"] is False
    assert json.loads(await pt._plaky_create_tasks_deferred("[]"))["ok"] is False
    out = json.loads(
        await pt._plaky_create_tasks_deferred(json.dumps([{"description": "no title"}]))
    )
    assert out["ok"] is False and "title" in out["message"]


@pytest.mark.asyncio
async def test_nothing_is_queued_when_validation_fails(db) -> None:
    from boardman.agent.tools import plaky_tools as pt

    await pt._plaky_create_tasks_deferred(json.dumps([{"description": "no title"}]))
    async with db() as session:
        rows = (
            (await session.execute(__import__("sqlalchemy").select(BackgroundJob))).scalars().all()
        )
    assert rows == []
