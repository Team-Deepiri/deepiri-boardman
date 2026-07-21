from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import (
    Base,
    IssueTaskMap,
    OpenPRTrack,
    PullRequestTaskLink,
    SyncLog,
)
from boardman.planning.huddle.context_sync import (
    SyncPlanningContext,
    repo_matches_team,
)


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_repo_matches_team_bare_and_full() -> None:
    assert repo_matches_team("deepiri-platform", ["deepiri-platform"])
    assert repo_matches_team("Team-Deepiri/deepiri-platform", ["deepiri-platform"])
    assert not repo_matches_team("other-repo", ["deepiri-platform"])


@pytest.mark.asyncio
async def test_sync_context_empty_db() -> None:
    _, factory = await _memory_session_factory()
    ctx = SyncPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
    )
    md = await ctx._context_markdown_async("qa")
    assert "No boardman sync history" in md


@pytest.mark.asyncio
async def test_sync_context_includes_pr_links_and_issues() -> None:
    _, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="deepiri-platform",
                github_pr_number=42,
                github_issue_number=7,
                plaky_task_id="plaky-1",
                link_source="issue_keyword",
            )
        )
        session.add(
            IssueTaskMap(
                github_repo="deepiri-platform",
                github_issue_number=7,
                plaky_task_id="plaky-1",
                plaky_task_url="https://plaky.example/t/plaky-1",
            )
        )
        session.add(
            OpenPRTrack(
                repo_full_name="Team-Deepiri/deepiri-platform",
                pr_number=99,
                plaky_item_id="plaky-qa-1",
                pr_title="Fix auth flow",
                pr_url="https://github.com/example/pull/99",
            )
        )
        session.add(
            SyncLog(
                action="pr_opened",
                github_repo="deepiri-platform",
                github_ref="42",
                plaky_task_id="plaky-1",
            )
        )
        await session.commit()

    ctx = SyncPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
    )
    md = await ctx._context_markdown_async("qa")
    assert "## Boardman Sync State" in md
    assert "### PR ↔ Plaky task links" in md
    assert "plaky-1" in md
    assert "deepiri-platform#42" in md
    assert "### Issue ↔ Plaky mappings" in md
    assert "deepiri-platform#7" in md
    assert "### Open PR tracks (QA pipeline)" in md
    assert "Fix auth flow" in md
    assert "### Recent webhook sync activity" in md
    assert "pr_opened: 1" in md


@pytest.mark.asyncio
async def test_sync_context_filters_other_team_repos() -> None:
    _, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="other-repo",
                github_pr_number=1,
                plaky_task_id="other-task",
            )
        )
        await session.commit()

    ctx = SyncPlanningContext(
        session_factory=factory,
        team_repos={"qa": ["deepiri-platform"]},
    )
    md = await ctx._context_markdown_async("qa")
    assert "other-task" not in md
    assert "No boardman sync history" in md
