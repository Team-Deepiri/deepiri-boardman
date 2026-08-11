"""PR ↔ Plaky linking pipeline: candidates, scoring, and run_pr_task_pipeline."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, SyncLog
from boardman.services.pr_task_linking import (
    TaskCandidate,
    format_triage_comment,
    github_head_ref,
    referenced_issue_numbers,
    run_pr_task_pipeline,
    score_candidate,
)
from boardman.settings import settings


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_cosine_word_similarity_identical():
    from boardman.services.pr_task_linking import _cosine_word_similarity

    assert _cosine_word_similarity("hello world", "world hello") == pytest.approx(1.0, rel=1e-6)


def test_github_head_ref():
    assert github_head_ref({"ref": "feature/foo"}) == "feature/foo"
    assert github_head_ref(None) == ""


def test_referenced_issue_numbers_branch_and_url():
    ref = referenced_issue_numbers(
        repo_full="deepiri-org/boardman",
        pr_title="Fix",
        pr_body="See https://github.com/deepiri-org/boardman/issues/7",
        head_ref="feature/42-bugfix",
    )
    assert 7 in ref
    assert 42 in ref


def test_referenced_issue_numbers_hash():
    ref = referenced_issue_numbers(
        repo_full="o/r",
        pr_title="x",
        pr_body="Maybe related to #99",
        head_ref="main",
    )
    assert 99 in ref


def test_score_overlap_and_mismatch():
    c = TaskCandidate(
        task_id="t1",
        title="[boardman] Fix auth",
        description="",
        issue_numbers={42},
        sources=["map"],
    )
    hi = score_candidate(
        c,
        ref_issues={42},
        pr_title="auth",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
    )
    assert hi.breakdown.get("issue_ref_overlap") == 100.0
    assert hi.score >= 100.0

    lo = score_candidate(
        c,
        ref_issues={99},
        pr_title="unrelated",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
    )
    assert lo.breakdown.get("issue_ref_mismatch") == -55.0


def test_format_triage_comment():
    from boardman.services.pr_task_linking import ScoredCandidate

    top = [
        ScoredCandidate("a", "t1", "", 55.0, {}),
        ScoredCandidate("b", "t2", "", 52.0, {}),
    ]
    txt = format_triage_comment(top)
    assert "t1" in txt
    assert "55.0" in txt


def test_score_assignee_identity_match():
    """Test that PR author matching task assignee adds score boost."""
    # Case 1: PR author matches assignee via email
    c = TaskCandidate(
        task_id="t1",
        title="Fix auth bug",
        description="",
        issue_numbers=set(),
        sources=["board_item"],
        assignee_login="alice",
        assignee_email="alice@company.com",
        assignee_name="Alice Smith",
    )
    result = score_candidate(
        c,
        ref_issues=set(),
        pr_title="Fix auth bug",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
        pr_author_login="alice",
        pr_author_email="alice@company.com",
        pr_author_name="Alice",
    )
    # identity_match should detect the email match and add score
    assert (
        result.breakdown.get("assignee_identity_match") == 50.0
        or result.breakdown.get("assignee_identity_partial") == 30.0
        or result.breakdown.get("assignee_identity_weak") == 15.0
    )


def test_score_pr_title_name_boost():
    """Test that PR title containing assignee name adds small boost."""
    c = TaskCandidate(
        task_id="t1",
        title="Fix for Alice",
        description="",
        issue_numbers=set(),
        sources=["board_item"],
        assignee_login="",
        assignee_email="",
        assignee_name="Alice Smith",
    )
    result = score_candidate(
        c,
        ref_issues=set(),
        pr_title="Fix for Alice Smith - auth bug",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
        pr_author_login="bob",
        pr_author_name="Bob",
    )
    # Should have pr_title_name_mention boost
    assert result.breakdown.get("pr_title_name_mention") == 20.0


def test_score_status_weighting():
    """Test that active status boosts and closed status penalizes score."""
    c_active = TaskCandidate(
        task_id="t1",
        title="Fix bug",
        description="",
        status="In Progress",
    )
    res_active = score_candidate(
        c_active,
        ref_issues=set(),
        pr_title="Fix bug",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
    )
    assert res_active.breakdown.get("status_active_boost") == 15.0

    c_done = TaskCandidate(
        task_id="t2",
        title="Fix bug",
        description="",
        status="Done",
    )
    res_done = score_candidate(
        c_done,
        ref_issues=set(),
        pr_title="Fix bug",
        pr_body="",
        repo_full="x/boardman",
        pr_number=1,
        session_penalty=False,
    )
    assert res_done.breakdown.get("status_closed_penalty") == -30.0


@pytest.mark.asyncio
async def test_pipeline_disabled(monkeypatch):
    monkeypatch.setattr(settings, "pr_linking_pipeline_enabled", False)
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        r = await run_pr_task_pipeline(
            session=session,
            plaky=object(),  # unused
            repo_full="a/b",
            repo_name="b",
            org="a",
            pr_number=1,
            pr_title="x",
            pr_body="",
            head={},
        )
    assert r.decision == "none"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_auto_link_db_and_branch(monkeypatch):
    monkeypatch.setattr(settings, "pr_linking_pipeline_enabled", True)
    monkeypatch.setattr(settings, "pr_linking_fetch_board_items", False)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {"ok": True, "items": []}

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            IssueTaskMap(
                github_repo="boardman",
                github_issue_number=42,
                plaky_task_id="plaky-42",
                plaky_task_url="https://plaky.example/i/42",
            )
        )
        await session.commit()

    async with factory() as session:
        r = await run_pr_task_pipeline(
            session=session,
            plaky=FakePlaky(),
            repo_full="deepiri-org/boardman",
            repo_name="boardman",
            org="deepiri-org",
            pr_number=100,
            pr_title="Implement feature",
            pr_body="",
            head={"ref": "feature/42-add-auth"},
        )

    assert r.decision == "auto_link"
    assert r.task_id == "plaky-42"
    assert r.score >= 90.0
    await engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_triage_below_medium(monkeypatch):
    monkeypatch.setattr(settings, "pr_linking_pipeline_enabled", True)
    monkeypatch.setattr(settings, "pr_linking_fetch_board_items", False)
    monkeypatch.setattr(settings, "pr_linking_llm_enabled", False)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {"ok": True, "items": []}

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            IssueTaskMap(
                github_repo="z",
                github_issue_number=1,
                plaky_task_id="other",
                plaky_task_url="",
            )
        )
        await session.commit()

    async with factory() as session:
        r = await run_pr_task_pipeline(
            session=session,
            plaky=FakePlaky(),
            repo_full="deepiri-org/boardman",
            repo_name="boardman",
            org="deepiri-org",
            pr_number=2,
            pr_title="Something else",
            pr_body="no overlap",
            head={"ref": "main"},
        )

    assert r.decision in ("triage", "none")
    await engine.dispose()


@pytest.mark.asyncio
async def test_score_penalty_other_pr(monkeypatch):
    monkeypatch.setattr(settings, "pr_linking_pipeline_enabled", True)
    monkeypatch.setattr(settings, "pr_linking_fetch_board_items", False)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {"ok": True, "items": []}

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            IssueTaskMap(
                github_repo="boardman",
                github_issue_number=42,
                plaky_task_id="plaky-42",
                plaky_task_url="",
            )
        )
        session.add(
            SyncLog(
                action="pr_linked",
                github_repo="boardman",
                github_ref="50",
                plaky_task_id="plaky-42",
                detail="{}",
            )
        )
        await session.commit()

    async with factory() as session:
        r = await run_pr_task_pipeline(
            session=session,
            plaky=FakePlaky(),
            repo_full="deepiri-org/boardman",
            repo_name="boardman",
            org="deepiri-org",
            pr_number=51,
            pr_title="Another PR",
            pr_body="",
            head={"ref": "feature/42-x"},
        )

    assert r.decision == "triage"
    assert any(
        s.breakdown.get("other_pr_linked") == -40.0 for s in r.top_scored if s.task_id == "plaky-42"
    )
    await engine.dispose()
