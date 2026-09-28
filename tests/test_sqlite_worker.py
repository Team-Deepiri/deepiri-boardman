"""Worker-side accounting for the stale-PR @mention sweep.

`sweep_due_nudges` returns an entry both for a comment it posted and for a PR that had
already merged/closed, where the escalation row was deleted instead. The worker used to
report `len(results)` as "sent", so a sweep that suppressed a nudge looked identical to
one that @mentioned somebody -- the exact line an operator reads while asking why a
merged PR was (or wasn't) being mentioned. These tests pin the two numbers apart, and
the last one runs the real sweep so the result shape they read can't drift silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.github import pr_actions
from boardman.services import pr_review_nudges as nudges
from boardman.sqlite_worker import _nudge_sweep_counts

# Entry shapes as `sweep_due_nudges` actually builds them.
SENT = {"github_repo": "r", "github_pr_number": 1, "days_waiting": 3, "stage": 1, "ok": True}
RETIRED = {
    "github_repo": "r",
    "github_pr_number": 2,
    "days_waiting": 12,
    "stage": 2,
    "ok": True,
    "skipped": "pr_not_active",
    "retired": True,
}
NO_RECIPIENT = {
    "github_repo": "r",
    "github_pr_number": 3,
    "days_waiting": 3,
    "stage": 1,
    "ok": True,
    "skipped": "no_recipient",
}


def test_nothing_due_is_nothing_sent():
    assert _nudge_sweep_counts([]) == (0, 0)


def test_a_posted_nudge_counts_as_sent():
    assert _nudge_sweep_counts([SENT]) == (1, 0)


def test_a_retired_pr_is_not_counted_as_sent():
    """The bug: `len(results)` reported a suppressed nudge as one that was sent."""
    sent, retired = _nudge_sweep_counts([RETIRED])
    assert sent == 0, "nothing left this process; a merged PR got no comment"
    assert retired == 1


def test_sent_and_retired_are_counted_separately():
    assert _nudge_sweep_counts([SENT, RETIRED, NO_RECIPIENT]) == (1, 1)


def test_a_skipped_row_is_neither_sent_nor_retired():
    """No recipient: nothing was said and nothing was deleted, so both stay honest."""
    assert _nudge_sweep_counts([NO_RECIPIENT]) == (0, 0)


def test_a_retired_false_flag_is_not_counted_as_a_retirement():
    assert _nudge_sweep_counts([{**RETIRED, "retired": False}]) == (0, 0)


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_sweep_output_splits_into_sent_and_retired(db_session, monkeypatch):
    """Run the actual sweep: one due open PR, one due merged PR, one PR not due yet.

    Guards the result shape the worker reads, which is the thing that silently rotted
    when terminal rows started appearing in the results list.
    """
    open_pr, merged_pr, not_due_pr = 11, 12, 13
    for number, days in ((open_pr, 3), (merged_pr, 3), (not_due_pr, 1)):
        await nudges.ensure_tracked(
            db_session,
            github_repo="deepiri-mudspeed",
            github_pr_number=number,
            developer_login="connorwhite9",
            primary_qa_login="sergiovargas111",
        )
        row = await nudges._get_row(db_session, "deepiri-mudspeed", number)
        row.last_activity_at = datetime.utcnow() - timedelta(days=days)
    await db_session.commit()

    # Keyed by PR number: the sweep hands GitHub a full owner/repo name, which is not
    # what this test cares about.
    state = {open_pr: pr_actions.PR_ACTIVE, merged_pr: pr_actions.PR_TERMINAL}

    async def fake_state(full_name: str, pr_number: int) -> str:
        return state[pr_number]

    posted: list[tuple] = []

    async def fake_comment(full_name, pr_number, body):
        posted.append((full_name, pr_number, body))
        return {"ok": True}

    monkeypatch.setattr(nudges, "pr_lifecycle_state", fake_state)
    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)

    results = await nudges.sweep_due_nudges(db_session)
    await db_session.commit()

    assert _nudge_sweep_counts(results) == (1, 1)
    assert [p[1] for p in posted] == [open_pr], "only the open PR may be mentioned"
    assert "@sergiovargas111" in posted[0][2]
    assert await nudges._get_row(db_session, "deepiri-mudspeed", merged_pr) is None
    assert await nudges._get_row(db_session, "deepiri-mudspeed", not_due_pr) is not None
