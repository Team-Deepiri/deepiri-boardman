"""Stale-PR @mention escalation: pure stage math + DB-backed activity tracking, no
GitHub calls except the mocked `pr_lifecycle_state`/`comment_on_pr` in the sweep tests.

Live failure (2026-09-26): the sweep read only its own table, and nothing removed a row
when the PR behind it finished, so deepiri-mudspeed#50 — merged 2026-09-14 — was
@mentioned again at days 3, 6, 9 and 12. `test_sweep_never_nudges_a_merged_pr` is that
bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, PrReviewNudge
from boardman.github import pr_actions
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


@pytest.fixture()
def pr_state(monkeypatch):
    """Stub the authoritative PR-state read. Defaults to open; override per test.

    The sweep asks GitHub whether a PR is still active before it @mentions anyone on it,
    so every sweep test has to say what that answer is. Recording the calls also lets a
    test assert that a row that is not due still costs zero GitHub calls.
    """
    calls: list[tuple[str, int]] = []
    state = {"value": pr_actions.PR_ACTIVE}

    async def fake_state(full_name: str, pr_number: int) -> str:
        calls.append((full_name, int(pr_number)))
        return state["value"]

    monkeypatch.setattr(nudges, "pr_lifecycle_state", fake_state)
    return SimpleNamespace(calls=calls, state=state)


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
async def test_sweep_sends_nudge_and_advances_stage(db_session, monkeypatch, pr_state):
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
    # Exactly one state read, for the one row that was actually due.
    assert pr_state.calls == [("Team-Deepiri/boardman", 7)]

    row = await nudges._get_row(db_session, "boardman", 7)
    assert row.nudge_stage == 1
    assert row.last_nudge_at is not None


@pytest.mark.asyncio
async def test_sweep_skips_when_not_yet_due(db_session, monkeypatch, pr_state):
    await _tracked(db_session)
    row = await nudges._get_row(db_session, "boardman", 7)
    row.last_activity_at = datetime.utcnow() - timedelta(days=1)
    await db_session.commit()

    async def fail_comment(*a, **kw):
        raise AssertionError("must not post a comment before day 3")

    monkeypatch.setattr(nudges, "comment_on_pr", fail_comment)
    results = await nudges.sweep_due_nudges(db_session)
    assert results == []
    # The whole point of the table is that a PR nobody is waiting on yet costs nothing.
    assert pr_state.calls == []


@pytest.mark.asyncio
async def test_sweep_does_not_resend_same_stage_twice(db_session, monkeypatch, pr_state):
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
async def test_sweep_skips_without_erroring_when_no_recipient(db_session, monkeypatch, pr_state):
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


# --- the merged-PR regression (deepiri-mudspeed#50) -------------------------------------


async def _due_soon(db_session, pr_number=50, *, dev="connorwhite9", qa="sergiovargas111"):
    """Track a PR exactly as `_assign_qa_for_pr` does, then age it into the day-3 window."""
    await nudges.ensure_tracked(
        db_session,
        github_repo="deepiri-mudspeed",
        github_pr_number=pr_number,
        developer_login=dev,
        primary_qa_login=qa,
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, "deepiri-mudspeed", pr_number)
    row.last_activity_at = datetime.utcnow() - timedelta(days=3)
    await db_session.commit()
    return row


def _no_comment(monkeypatch):
    """Fail loudly if anything tries to @mention a human."""
    posted: list[tuple] = []

    async def fail_comment(full_name, pr_number, body):
        posted.append((full_name, pr_number, body))
        raise AssertionError(f"must not comment on {full_name}#{pr_number}: {body!r}")

    monkeypatch.setattr(nudges, "comment_on_pr", fail_comment)
    return posted


@pytest.mark.asyncio
async def test_sweep_never_nudges_a_merged_pr(db_session, monkeypatch, pr_state):
    """The reported bug: PR merged, Boardman kept @mentioning the QA engineer.

    On mudspeed#50 the merge landed 2026-09-14 and the sweep still nudged at days 3, 6, 9
    and 12. The escalation row said "waiting on QA" and nothing in the DB knows the PR is
    gone, so the only place that can say no is a live read of the PR.
    """
    await _due_soon(db_session)
    posted = _no_comment(monkeypatch)
    pr_state.state["value"] = pr_actions.PR_TERMINAL

    results = await nudges.sweep_due_nudges(db_session)

    assert posted == []
    assert results and results[0]["skipped"] == "pr_not_active"
    assert results[0]["retired"] is True
    # Retired, not just skipped: nothing is waiting on a merged PR any more.
    assert await nudges._get_row(db_session, "deepiri-mudspeed", 50) is None


@pytest.mark.asyncio
async def test_sweep_never_nudges_a_closed_unmerged_pr(db_session, monkeypatch, pr_state):
    """Abandoned PRs are terminal too -- a comment asking for a review nobody can give."""
    await _due_soon(db_session, pr_number=51)
    posted = _no_comment(monkeypatch)
    pr_state.state["value"] = pr_actions.PR_TERMINAL

    results = await nudges.sweep_due_nudges(db_session)

    assert posted == []
    assert results[0]["skipped"] == "pr_not_active"
    assert await nudges._get_row(db_session, "deepiri-mudspeed", 51) is None


@pytest.mark.asyncio
async def test_merged_pr_is_never_nudged_again_on_later_sweeps(db_session, monkeypatch, pr_state):
    """Retirement is durable: a merged PR must not reappear at the day-6, 9, 12 marks."""
    await _due_soon(db_session)
    posted = _no_comment(monkeypatch)
    pr_state.state["value"] = pr_actions.PR_TERMINAL

    first = await nudges.sweep_due_nudges(db_session)
    assert first and first[0]["skipped"] == "pr_not_active"

    # Every later sweep is silent: the row is gone, so there is nothing left to be due.
    for _ in range(4):
        assert await nudges.sweep_due_nudges(db_session) == []

    assert posted == []
    assert len(pr_state.calls) == 1, "a retired PR must not be re-read, let alone re-mentioned"
    assert await nudges._get_row(db_session, "deepiri-mudspeed", 50) is None


@pytest.mark.asyncio
async def test_unreadable_pr_state_skips_the_nudge_but_keeps_the_row(
    db_session, monkeypatch, pr_state
):
    """GitHub not answering must not be read as "merged" -- and must not be read as "open".

    Skipping is free (the next sweep retries); retiring on an unreadable state would
    silently drop escalation for a PR that is still open, and the row is the only
    record that anyone is waiting on anybody.
    """
    await _due_soon(db_session)
    posted = _no_comment(monkeypatch)
    pr_state.state["value"] = pr_actions.PR_STATE_UNKNOWN

    assert await nudges.sweep_due_nudges(db_session) == []

    assert posted == []
    row = await nudges._get_row(db_session, "deepiri-mudspeed", 50)
    assert row is not None
    assert row.nudge_stage == 0, "an unreadable state must not burn the escalation stage"

    # ...and the next sweep, with GitHub back, does the thing it should have.
    async def fake_comment(full_name, pr_number, body):
        posted.append((full_name, pr_number, body))
        return {"ok": True}

    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)
    pr_state.state["value"] = pr_actions.PR_ACTIVE
    results = await nudges.sweep_due_nudges(db_session)
    assert len(results) == 1 and results[0]["stage"] == 1
    assert "@sergiovargas111" in posted[0][2]


@pytest.mark.asyncio
async def test_open_pr_is_still_nudged(db_session, monkeypatch, pr_state):
    """The guard must not disable the feature it protects."""
    await _due_soon(db_session)
    pr_state.state["value"] = pr_actions.PR_ACTIVE

    posted: list[tuple] = []

    async def fake_comment(full_name, pr_number, body):
        posted.append((full_name, pr_number, body))
        return {"ok": True}

    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)
    results = await nudges.sweep_due_nudges(db_session)

    assert len(results) == 1
    assert "skipped" not in results[0]
    assert "@sergiovargas111" in posted[0][2]
    assert await nudges._get_row(db_session, "deepiri-mudspeed", 50) is not None


@pytest.mark.asyncio
async def test_merged_pr_is_not_nudged_even_on_the_daily_cadence(db_session, monkeypatch, pr_state):
    """Past day 15 the schedule is daily, so the observed bug never stopped on its own.

    mudspeed#50 sat 12 days stale when the report came in; a merged PR left alone would
    have been mentioned every day indefinitely.
    """
    pr_state.state["value"] = pr_actions.PR_TERMINAL
    posted = _no_comment(monkeypatch)

    row = await _due_soon(db_session)
    row.last_activity_at = datetime.utcnow() - timedelta(days=40)
    await db_session.commit()

    results = await nudges.sweep_due_nudges(db_session)
    assert posted == []
    assert results[0]["skipped"] == "pr_not_active"
    assert results[0]["days_waiting"] == 40
    assert await nudges._get_row(db_session, "deepiri-mudspeed", 50) is None


# --- retire -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_removes_a_terminal_prs_row(db_session):
    await _tracked(db_session)
    assert await nudges.retire(db_session, github_repo="boardman", github_pr_number=7) is True
    await db_session.commit()
    assert await nudges._get_row(db_session, "boardman", 7) is None


@pytest.mark.asyncio
async def test_retire_is_a_noop_for_an_untracked_pr(db_session):
    assert await nudges.retire(db_session, github_repo="boardman", github_pr_number=404) is False
    await db_session.commit()
