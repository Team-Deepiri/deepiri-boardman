"""PR ↔ Plaky registry for merge-gated completion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, PullRequestTaskLink
from boardman.services.pr_task_registry import (
    has_any_open_pr_for_task,
    mark_pr_merged,
    mark_pr_withdrawn,
    upsert_pr_task_link,
)


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_merge_gate_two_prs_one_task():
    engine, factory = await _session_factory()
    async with factory() as session:
        await upsert_pr_task_link(
            session,
            github_repo="boardman",
            github_pr_number=1,
            plaky_task_id="T1",
            github_issue_number=10,
            link_source="issue_keyword",
        )
        await upsert_pr_task_link(
            session,
            github_repo="boardman",
            github_pr_number=2,
            plaky_task_id="T1",
            github_issue_number=10,
            link_source="issue_keyword",
        )
        await session.commit()

    async with factory() as session:
        assert await has_any_open_pr_for_task(session, plaky_task_id="T1") is True
        await mark_pr_merged(session, github_repo="boardman", github_pr_number=1)
        await session.commit()

    async with factory() as session:
        assert await has_any_open_pr_for_task(session, plaky_task_id="T1") is True
        await mark_pr_merged(session, github_repo="boardman", github_pr_number=2)
        await session.commit()

    async with factory() as session:
        assert await has_any_open_pr_for_task(session, plaky_task_id="T1") is False


@pytest.mark.asyncio
async def test_withdrawn_pr_not_blocking():
    engine, factory = await _session_factory()
    async with factory() as session:
        await upsert_pr_task_link(
            session,
            github_repo="boardman",
            github_pr_number=5,
            plaky_task_id="T9",
            github_issue_number=0,
            link_source="auto_link",
        )
        await session.commit()

    async with factory() as session:
        await mark_pr_withdrawn(session, github_repo="boardman", github_pr_number=5)
        await session.commit()

    async with factory() as session:
        assert await has_any_open_pr_for_task(session, plaky_task_id="T9") is False
        r = await session.execute(select(PullRequestTaskLink).where(PullRequestTaskLink.github_pr_number == 5))
        row = r.scalar_one()
        assert row.withdrawn_at is not None
