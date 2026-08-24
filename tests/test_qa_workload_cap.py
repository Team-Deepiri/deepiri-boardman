"""QA workload cap: the picker skips overloaded reviewers and falls back to least-loaded.

Pins the invariant: a QA at or above qa_max_active_prs is deferred in favor of one
with capacity, and when every candidate is at cap, the least-loaded wins.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember
from boardman.database.models import Base, PullRequestTaskLink
from boardman.services.pr_task_registry import active_pr_counts_by_qa, stamp_qa_on_pr_links


def _member(mid: str, display: str, login: str = "") -> TeamMember:
    return TeamMember(
        id=mid,
        display=display,
        github_login=login or display.lower().replace(" ", ""),
        roles=["qa"],
        tier="standard",
        qa_tier=3,
        repo_globs=["*"],
        explicit_repos=[],
        weight=1.0,
    )


def _cfg(*members: TeamMember) -> TeamAssignmentsConfig:
    from boardman.assignment.config import AmbiguousPRConfig, QaRepoRules, TierSpec

    return TeamAssignmentsConfig(
        plaky_field_engineer="person-1",
        plaky_field_qa="person-2",
        plaky_field_repo="tag-1",
        plaky_field_github_repos="tag-1",
        tiers={"standard": TierSpec(name="standard", weight_bias=1.0)},
        members=list(members),
        heavy_repo_patterns=[],
        qa_repo_rules=QaRepoRules(),
        random_jitter=0.0,
        ambiguous_pr=AmbiguousPRConfig(),
        qa_excluded=[],
        developer_excluded=[],
        qa_bug_specialist="",
        fallback_members=[],
    )


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _link(repo: str, pr: int, task: str, qa: str | None = None, merged: bool = False):
    from datetime import datetime

    return PullRequestTaskLink(
        github_repo=repo,
        github_pr_number=pr,
        github_issue_number=0,
        plaky_task_id=task,
        link_source="issue_keyword",
        qa_plaky_id=qa,
        merged_at=datetime.utcnow() if merged else None,
    )


@pytest.mark.asyncio
async def test_active_pr_counts_by_qa(db_factory):
    """Counts only open (not merged, not withdrawn) PR links per QA."""
    async with db_factory() as session:
        session.add_all(
            [
                _link("repo", 1, "t1", qa="qa-alice"),
                _link("repo", 2, "t2", qa="qa-alice"),
                _link("repo", 3, "t3", qa="qa-bob"),
                _link("repo", 4, "t4", qa="qa-alice", merged=True),
            ]
        )
        await session.commit()

    async with db_factory() as session:
        counts = await active_pr_counts_by_qa(session)
        assert counts["qa-alice"] == 2
        assert counts["qa-bob"] == 1
        assert "qa-merged" not in counts


@pytest.mark.asyncio
async def test_stamp_qa_on_pr_links(db_factory):
    """Stamping QA sets qa_plaky_id on active link rows."""
    async with db_factory() as session:
        session.add(_link("repo", 10, "t10"))
        await session.commit()

    async with db_factory() as session:
        n = await stamp_qa_on_pr_links(
            session, github_repo="repo", github_pr_number=10, qa_plaky_id="qa-carol"
        )
        await session.commit()
        assert n == 1

    async with db_factory() as session:
        counts = await active_pr_counts_by_qa(session)
        assert counts.get("qa-carol") == 1


@pytest.mark.asyncio
async def test_picker_skips_overloaded_qa(monkeypatch):
    """A QA at cap is skipped; the one with capacity wins."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "qa_max_active_prs", 2)
    monkeypatch.setattr(bs.settings, "qa_github_fit_enabled", False)

    alice = _member("qa-alice", "Alice")
    bob = _member("qa-bob", "Bob")
    cfg = _cfg(alice, bob)

    from boardman.assignment.qa_picker import pick_qa_for_repo

    workload = {"qa-alice": 3, "qa-bob": 1}
    qid, why = await pick_qa_for_repo(
        "team/repo", cfg, exclude_login="someone-else", qa_workload=workload
    )
    assert qid == "qa-bob", f"Expected Bob (under cap) but got {qid}: {why}"


@pytest.mark.asyncio
async def test_picker_falls_back_to_least_loaded_when_all_at_cap(monkeypatch):
    """When every candidate is at cap, the least-loaded wins."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "qa_max_active_prs", 2)
    monkeypatch.setattr(bs.settings, "qa_github_fit_enabled", False)

    alice = _member("qa-alice", "Alice")
    bob = _member("qa-bob", "Bob")
    cfg = _cfg(alice, bob)

    from boardman.assignment.qa_picker import pick_qa_for_repo

    workload = {"qa-alice": 5, "qa-bob": 3}
    qid, why = await pick_qa_for_repo(
        "team/repo", cfg, exclude_login="someone-else", qa_workload=workload
    )
    assert qid == "qa-bob", f"Expected Bob (least loaded) but got {qid}: {why}"


@pytest.mark.asyncio
async def test_picker_assigns_when_no_workload_data(monkeypatch):
    """Without workload data, the picker works as before (no filtering)."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "qa_github_fit_enabled", False)

    alice = _member("qa-alice", "Alice")
    cfg = _cfg(alice)

    from boardman.assignment.qa_picker import pick_qa_for_repo

    qid, why = await pick_qa_for_repo("team/repo", cfg, exclude_login="someone-else")
    assert qid == "qa-alice"


@pytest.mark.asyncio
async def test_merged_prs_do_not_count_toward_workload(db_factory):
    """A merged PR does not count toward the QA's active workload."""
    async with db_factory() as session:
        session.add_all(
            [
                _link("repo", 1, "t1", qa="qa-alice", merged=True),
                _link("repo", 2, "t2", qa="qa-alice", merged=True),
                _link("repo", 3, "t3", qa="qa-alice"),
            ]
        )
        await session.commit()

    async with db_factory() as session:
        counts = await active_pr_counts_by_qa(session)
        assert counts["qa-alice"] == 1
