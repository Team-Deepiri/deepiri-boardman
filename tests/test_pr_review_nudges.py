"""Stale-PR @mention escalation: pure stage math + DB-backed activity tracking, no
GitHub calls except the mocked comment_on_pr in the sweep tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, PrReviewNudge
from boardman.services import pr_review_nudges as nudges


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- stage math --------------------------------------------------------------------


@pytest.mark.parametrize(
    "days,expected",
    [
        (0, 0),
        (2, 0),
        (3, 1),
        (5, 1),
        (6, 2),
        (8, 2),
        (9, 3),
        (12, 4),
        (14, 4),
        (15, 5),
        (16, 6),
        (17, 7),
        (30, 20),
    ],
)
def test_target_stage_schedule(days, expected):
    assert nudges._target_stage(days) == expected


# --- ensure_tracked ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_tracked_creates_baseline_row(db_session):
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=42,
        developer_login="Alice",
        primary_qa_login="Bob",
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 42)
    assert row is not None
    assert row.developer_login == "Alice"
    assert row.primary_qa_login == "Bob"
    assert row.waiting_on == "qa"
    assert row.nudge_stage == 0


@pytest.mark.asyncio
async def test_ensure_tracked_refreshes_logins_without_resetting_clock(db_session):
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=42,
        developer_login="Alice",
        primary_qa_login="Bob",
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 42)
    row.nudge_stage = 3
    row.waiting_on = "developer"
    await db_session.commit()

    # A re-assignment (new QA) must not reset an already-running escalation clock.
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=42,
        developer_login="Alice",
        primary_qa_login="Carol",
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 42)
    assert row.primary_qa_login == "Carol"
    assert row.nudge_stage == 3
    assert row.waiting_on == "developer"


@pytest.mark.asyncio
async def test_ensure_tracked_no_developer_login_is_a_noop(db_session):
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=1,
        developer_login="",
        primary_qa_login="Bob",
    )
    await db_session.commit()
    assert await nudges._get_row(db_session, "boardman", 1) is None


# --- record_activity -----------------------------------------------------------------


async def _tracked(db_session, *, dev="alice", qa="bob"):
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=7,
        developer_login=dev,
        primary_qa_login=qa,
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_record_activity_untracked_pr_is_noop(db_session):
    await nudges.record_activity(
        db_session,
        github_repo="boardman",
        github_pr_number=999,
        actor_login="alice",
        at=datetime.utcnow(),
    )
    assert await nudges._get_row(db_session, "boardman", 999) is None


@pytest.mark.asyncio
async def test_developer_action_flips_waiting_on_to_qa_and_resets_stage(db_session):
    await _tracked(db_session)
    row = await nudges._get_row(db_session, "boardman", 7)
    row.waiting_on = "developer"
    row.nudge_stage = 4
    row.last_nudge_at = datetime.utcnow()
    await db_session.commit()

    await nudges.record_activity(
        db_session,
        github_repo="boardman",
        github_pr_number=7,
        actor_login="Alice",  # developer, case-insensitive
        at=datetime.utcnow(),
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 7)
    assert row.waiting_on == "qa"
    assert row.nudge_stage == 0
    assert row.last_nudge_at is None


@pytest.mark.asyncio
async def test_qa_action_flips_waiting_on_to_developer(db_session):
    await _tracked(db_session)
    await nudges.record_activity(
        db_session,
        github_repo="boardman",
        github_pr_number=7,
        actor_login="bob",
        at=datetime.utcnow(),
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 7)
    assert row.waiting_on == "developer"


@pytest.mark.asyncio
async def test_drive_by_commenter_once_does_not_flip_turn(db_session):
    await _tracked(db_session)
    row_before = await nudges._get_row(db_session, "boardman", 7)
    waiting_before = row_before.waiting_on

    await nudges.record_activity(
        db_session,
        github_repo="boardman",
        github_pr_number=7,
        actor_login="random-passerby",
        at=datetime.utcnow(),
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 7)
    assert row.waiting_on == waiting_before  # unchanged


@pytest.mark.asyncio
async def test_commenter_chips_in_after_second_comment(db_session):
    await _tracked(db_session)
    for _ in range(2):
        await nudges.record_activity(
            db_session,
            github_repo="boardman",
            github_pr_number=7,
            actor_login="carol-helper",
            at=datetime.utcnow(),
        )
        await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 7)
    # Second comment counted as a QA-side action -> flips developer's way.
    assert row.waiting_on == "developer"
    recipients = nudges._recipients(
        PrReviewNudge(
            github_repo="boardman",
            github_pr_number=7,
            developer_login=row.developer_login,
            primary_qa_login=row.primary_qa_login,
            extra_qa_json=row.extra_qa_json,
            waiting_on="qa",
            last_activity_at=row.last_activity_at,
        )
    )
    assert "carol-helper" in recipients
    assert "bob" in recipients


# --- sweep_due_nudges ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_sends_nudge_and_advances_stage(db_session, monkeypatch):
    await _tracked(db_session)
    row = await nudges._get_row(db_session, "boardman", 7)
    row.last_activity_at = datetime.utcnow() - timedelta(days=4)
    await db_session.commit()

    calls = []

    async def fake_comment(full_name, pr_number, body):
        calls.append((full_name, pr_number, body))
        return {"ok": True}

    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)
    results = await nudges.sweep_due_nudges(db_session)
    assert len(results) == 1
    assert results[0]["stage"] == 1
    assert "@bob" in calls[0][2]

    row = await nudges._get_row(db_session, "boardman", 7)
    assert row.nudge_stage == 1
    assert row.last_nudge_at is not None


@pytest.mark.asyncio
async def test_sweep_skips_when_not_yet_due(db_session, monkeypatch):
    await _tracked(db_session)
    row = await nudges._get_row(db_session, "boardman", 7)
    row.last_activity_at = datetime.utcnow() - timedelta(days=1)
    await db_session.commit()

    async def fail_comment(*a, **kw):
        raise AssertionError("must not post a comment before day 3")

    monkeypatch.setattr(nudges, "comment_on_pr", fail_comment)
    results = await nudges.sweep_due_nudges(db_session)
    assert results == []


@pytest.mark.asyncio
async def test_sweep_does_not_resend_same_stage_twice(db_session, monkeypatch):
    await _tracked(db_session)
    row = await nudges._get_row(db_session, "boardman", 7)
    row.last_activity_at = datetime.utcnow() - timedelta(days=4)
    await db_session.commit()

    async def fake_comment(full_name, pr_number, body):
        return {"ok": True}

    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)
    first = await nudges.sweep_due_nudges(db_session)
    assert len(first) == 1
    second = await nudges.sweep_due_nudges(db_session)
    assert second == []


@pytest.mark.asyncio
async def test_sweep_skips_without_erroring_when_no_recipient(db_session, monkeypatch):
    await nudges.ensure_tracked(
        db_session,
        github_repo="boardman",
        github_pr_number=8,
        developer_login="alice",
        primary_qa_login="",
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "boardman", 8)
    row.last_activity_at = datetime.utcnow() - timedelta(days=4)
    await db_session.commit()

    async def fail_comment(*a, **kw):
        raise AssertionError("must not post with no resolvable recipient")

    monkeypatch.setattr(nudges, "comment_on_pr", fail_comment)
    results = await nudges.sweep_due_nudges(db_session)
    assert results == []
