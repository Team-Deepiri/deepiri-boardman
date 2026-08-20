"""Reporting creation as done is only safe if a failure is not silent.

The receipt says "here is what I created" because the write lands within seconds.
When one does not land, the next turn has to open by correcting it rather than leaving
the user believing a task exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.write_failures import recent_failed_task_writes
from boardman.database.models import BackgroundJob, Base


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _job(**kw) -> BackgroundJob:
    base = dict(
        id=kw.pop("id", "j1"),
        kind="plaky_create_tasks_job",
        payload_json=json.dumps({"tasks": [{"title": "Ship the sync"}]}),
        status="incomplete",
        success=False,
        result_json=json.dumps({"error": "plaky 500"}),
        finished_at=datetime.utcnow(),
    )
    base.update(kw)
    return BackgroundJob(**base)


@pytest.mark.asyncio
async def test_a_failed_write_is_named_for_correction(session) -> None:
    session.add(_job())
    await session.flush()
    out = await recent_failed_task_writes(session)
    assert "Ship the sync" in out
    assert "not created" in out
    assert "correcting" in out.lower() or "correct" in out.lower()


@pytest.mark.asyncio
async def test_nothing_is_said_when_writes_succeeded(session) -> None:
    session.add(_job(status="complete", success=True, result_json=json.dumps({"ok": True})))
    await session.flush()
    assert await recent_failed_task_writes(session) == ""


@pytest.mark.asyncio
async def test_an_old_failure_is_not_re_raised_forever(session) -> None:
    session.add(_job(finished_at=datetime.utcnow() - timedelta(hours=3)))
    await session.flush()
    assert await recent_failed_task_writes(session, minutes=30) == ""


@pytest.mark.asyncio
async def test_a_still_running_write_is_not_reported_as_failed(session) -> None:
    session.add(_job(status="running", success=None, result_json=None, finished_at=None))
    await session.flush()
    assert await recent_failed_task_writes(session) == ""


@pytest.mark.asyncio
async def test_titles_fall_back_to_the_failed_rows_in_the_result(session) -> None:
    session.add(
        _job(
            payload_json=json.dumps({}),
            result_json=json.dumps({"results": [{"ok": False, "title": "Rate limit the webhook"}]}),
        )
    )
    await session.flush()
    assert "Rate limit the webhook" in await recent_failed_task_writes(session)
