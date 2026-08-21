"""A task must not be dragged backwards through the workflow by an ownership event.

"Who owns this issue" and "where has the work got to" are different questions. A GitHub
`assigned` event answers the first, and it is true whether QA is halfway through the PR
or nobody has started. Writing the assignee-derived status unconditionally answered the
second question with the first question's answer: assigning a developer to an issue whose
PR was already In QA wrote Assigned over it, and QA's work vanished from the board.

Deliberate transitions are unaffected. QA rejecting, a merge completing, an issue closing
-- those are statements about where the work now is, and they move it in either direction
because that is what they are for.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap
from boardman.github.webhooks import IssueEventPayload
from boardman.services import issue_handler as ih
from boardman.services.sync_state import (
    ASSIGNEE_DERIVED_INTENTS,
    WORKFLOW_RANK,
    status_intent_would_regress,
    workflow_rank,
)

REPO = "deepiri-boardman"
FULL = "Team-Deepiri/deepiri-boardman"
TASK = "7209283"
BOARD = "269031"


# --- the ordering itself ---------------------------------------------------------------


def test_the_workflow_order_is_the_one_the_team_agreed() -> None:
    order = [
        "workflow_needs_assigned",
        "workflow_assigned",
        "workflow_in_progress",
        "workflow_needs_qa",
        "workflow_in_qa",
        "github_pr_review_approved",
        "workflow_completed",
    ]
    ranks = [workflow_rank(i) for i in order]
    assert all(r is not None for r in ranks)
    assert ranks == sorted(ranks), ranks
    assert len(set(ranks)) == len(ranks), "each of these is a distinct position"


def test_pausing_is_not_a_demotion() -> None:
    """Pausing says the work continues later, not that it went back to unowned."""
    assert workflow_rank("workflow_paused") == workflow_rank("workflow_in_progress")


def test_needing_qa_again_is_the_same_position_as_needing_qa() -> None:
    assert workflow_rank("workflow_needs_qa_again") == workflow_rank("workflow_needs_qa")


@pytest.mark.parametrize("current", sorted(WORKFLOW_RANK))
def test_only_assignee_derived_intents_are_ever_held_back(current: str) -> None:
    for nxt in WORKFLOW_RANK:
        if nxt in ASSIGNEE_DERIVED_INTENTS:
            continue
        assert (
            status_intent_would_regress(current, nxt) is False
        ), f"{nxt} is a deliberate transition and must never be blocked"


@pytest.mark.parametrize(
    ("current", "blocked"),
    [
        ("workflow_in_qa", True),
        ("workflow_needs_qa", True),
        ("workflow_in_progress", True),
        ("github_pr_review_approved", True),
        ("workflow_completed", True),
        ("workflow_assigned", False),
        ("workflow_needs_assigned", False),
    ],
)
def test_assigning_never_moves_a_task_backwards(current: str, blocked: bool) -> None:
    assert status_intent_would_regress(current, "workflow_assigned") is blocked


def test_an_unreadable_current_status_never_blocks_the_write() -> None:
    """A board whose vocabulary this code cannot place is not evidence of anything."""
    assert status_intent_would_regress("", "workflow_assigned") is False
    assert status_intent_would_regress("something-a-board-invented", "workflow_assigned") is False


# --- the handler ------------------------------------------------------------------------


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(IssueTaskMap(github_repo=REPO, github_issue_number=94, plaky_task_id=TASK))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture()
def board(monkeypatch):
    """A board whose status ids are the intent names, so assertions read plainly."""
    written: list[Any] = []

    class Routing:
        plaky_board_id = BOARD
        plaky_group_id = "g1"
        plaky_table = ""
        category = ""

    async def fake_routing(*_a, **_k):
        return Routing()

    async def fake_resolve(board_id, *, intent):
        return ("status_key", intent)

    async def fake_update(task_id, inp):
        written.append(inp)
        return {"ok": True, "operations": {}}

    async def fake_engineer(login):
        return f"plaky:{login}" if login else ""

    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)
    monkeypatch.setattr(ih, "_resolve_issue_engineer_id", fake_engineer)
    return written


def _at_status(monkeypatch, intent: str) -> None:
    async def fake_current(_board_id, _task_id, _field_key):
        return intent

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.current_status_intent", fake_current)


def _issue_event(action: str, *, assignee: str = "ali-ferris", state: str = "open"):
    return IssueEventPayload(
        action=action,
        issue={
            "number": 94,
            "title": "Retry the flaky sync",
            "body": "b",
            "state": state,
            "html_url": f"https://github.com/{FULL}/issues/94",
            "assignees": [{"login": assignee}] if assignee else [],
            "labels": [],
        },
        repository={"full_name": FULL, "name": REPO},
    )


@pytest.mark.asyncio
async def test_assigning_a_developer_does_not_reset_a_task_in_qa(
    db_session, board, monkeypatch
) -> None:
    """The reproduction: QA is reviewing, someone assigns a dev, the board said Assigned."""
    _at_status(monkeypatch, "workflow_in_qa")

    res = await ih.handle_issue_changed(_issue_event("assigned"), db_session)

    assert res["status"] is None, "no status written"
    assert res["status_held_back"] == "workflow_in_qa"
    assert board and board[0].status is None
    assert board[0].engineer_plaky_id == "plaky:ali-ferris", "the assignee still lands"


@pytest.mark.asyncio
async def test_assigning_still_moves_an_unowned_task_to_assigned(
    db_session, board, monkeypatch
) -> None:
    """The guard must not stop the case it was never about."""
    _at_status(monkeypatch, "workflow_needs_assigned")

    res = await ih.handle_issue_changed(_issue_event("assigned"), db_session)

    assert res["status"] == "workflow_assigned"
    assert res["status_held_back"] is None


@pytest.mark.asyncio
async def test_unassigning_does_not_reset_a_task_in_qa(db_session, board, monkeypatch) -> None:
    _at_status(monkeypatch, "workflow_in_qa")

    res = await ih.handle_issue_changed(_issue_event("unassigned", assignee=""), db_session)

    assert res["status"] is None
    assert res["status_held_back"] == "workflow_in_qa"


@pytest.mark.asyncio
async def test_closing_an_issue_still_completes_it_from_any_position(
    db_session, board, monkeypatch
) -> None:
    """Closing is a statement about where the work IS. It is never held back."""
    _at_status(monkeypatch, "workflow_in_qa")

    res = await ih.handle_issue_changed(
        _issue_event("closed", state="closed"), db_session, event_label="issue_closed"
    )

    assert res["status"] == "workflow_completed"
    assert res["status_held_back"] is None


@pytest.mark.asyncio
async def test_a_label_change_writes_no_status_at_all(db_session, board, monkeypatch) -> None:
    """Unchanged behaviour, pinned: a label knows nothing about workflow position."""
    _at_status(monkeypatch, "workflow_in_qa")

    res = await ih.handle_issue_changed(_issue_event("labeled"), db_session)

    assert res["status"] is None
    assert res["status_held_back"] is None, "nothing was even considered"


@pytest.mark.asyncio
async def test_the_guard_is_idempotent_across_repeated_deliveries(
    db_session, board, monkeypatch
) -> None:
    _at_status(monkeypatch, "workflow_in_qa")
    event = _issue_event("assigned")

    first = await ih.handle_issue_changed(event, db_session)
    second = await ih.handle_issue_changed(event, db_session)

    assert first["status_held_back"] == second["status_held_back"] == "workflow_in_qa"
    assert all(inp.status is None for inp in board)


# --- the PR metadata path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_pr_edit_does_not_reset_a_task_in_qa(db_session, monkeypatch) -> None:
    """A draft PR resolves to Assigned. Writing that over a task QA is reviewing loses
    QA's position, exactly as the issue path did on `assigned`."""
    from boardman.github.webhooks import PullRequestEventPayload
    from boardman.services import pr_handler as ph

    written: list[Any] = []

    class Routing:
        plaky_board_id = BOARD
        plaky_group_id = "g1"

    async def fake_routing(*_a, **_k):
        return Routing()

    async def fake_resolve(board_id, *, intent):
        return ("status_key", intent)

    async def fake_current(_board_id, _task_id, _field_key):
        return "workflow_in_qa"

    async def fake_update(task_id, inp):
        written.append(inp)
        return {"ok": True}

    async def task_ids(session, *, github_repo, github_pr_number):
        return [TASK]

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.current_status_intent", fake_current)
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", task_ids)

    payload = PullRequestEventPayload(
        action="edited",
        pull_request={
            "number": 88,
            "title": "t",
            "body": "no issue reference",
            "html_url": f"https://github.com/{FULL}/pull/88",
            "state": "open",
            "merged": False,
            "draft": True,
            "user": {"login": "ali-ferris"},
            "head": {"ref": "feat/x"},
        },
        repository={"full_name": FULL, "name": REPO},
    )
    res = await ph.handle_pr_edited(payload, db_session)

    assert res["event"] == "pr_metadata_synced"
    assert written and written[0].status is None, "In QA survives a draft-PR edit"
    assert written[0].task_type, "the rest of the metadata still syncs"
