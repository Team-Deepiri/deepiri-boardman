from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap
from boardman.github.webhooks import IssueEventPayload
from boardman.services import issue_handler
from boardman.services.priority_rules import infer_priority_from_text
from boardman.services.sync_state import resolve_issue_state, resolve_pr_state


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_issue_state_uses_native_type_priority_and_assignee() -> None:
    state = resolve_issue_state(
        {
            "number": 12,
            "title": "Fix login",
            "body": "Security regression",
            "html_url": "https://github.com/acme/app/issues/12",
            "type": {"name": "Bug"},
            "labels": [{"name": "priority: very important"}],
            "assignees": [{"login": "octocat"}],
        },
        repo_full_name="acme/app",
        repo_name="app",
    )

    assert state.task_type == "Bug"
    assert state.priority == "Very Important"
    assert state.assignee_login == "octocat"
    assert state.url.endswith("/12")


def test_pr_state_uses_label_type_and_author_as_default_owner() -> None:
    state = resolve_pr_state(
        {
            "number": 7,
            "title": "Improve parser",
            "body": "Routine cleanup",
            "html_url": "https://github.com/acme/app/pull/7",
            "user": {"login": "octocat"},
            "head": {"ref": "work/parser"},
            "labels": [{"name": "bug"}],
        },
        repo_full_name="acme/app",
        repo_name="app",
    )

    assert state.task_type == "Bug"
    assert state.priority == "Low"
    assert state.assignee_login == "octocat"


def test_priority_explicit_very_important_beats_text() -> None:
    assert infer_priority_from_text("docs cleanup", "", ["priority: very important"]) == (
        "Very Important"
    )


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Low", "Low"),
        ("priority: medium", "Medium"),
        ("priority-high", "High"),
        ("Urgent", "Very Important"),
        ("urgent-priority", "Very Important"),
    ],
)
def test_github_priority_labels_map_to_plaky_buckets(label: str, expected: str) -> None:
    assert infer_priority_from_text("neutral issue", labels=[label]) == expected


def test_issue_native_priority_value_is_preserved() -> None:
    state = resolve_issue_state(
        {
            "number": 13,
            "title": "Neutral issue",
            "html_url": "https://github.com/acme/app/issues/13",
            "priority": {"name": "High"},
            "labels": [],
        },
        repo_full_name="acme/app",
        repo_name="app",
    )
    assert state.priority == "High"


@pytest.mark.asyncio
async def test_issue_label_sync_recomputes_priority_and_type(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="app", github_issue_number=12, plaky_task_id="task-12"))
    await db_session.flush()
    captured: list[Any] = []

    async def fake_routing(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_update(task_id: str, req: Any) -> dict[str, Any]:
        captured.append((task_id, req))
        return {"ok": True}

    monkeypatch.setattr(issue_handler, "get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)

    payload = IssueEventPayload(
        action="labeled",
        issue={
            "number": 12,
            "title": "Security fix",
            "body": "Protect production",
            "html_url": "https://github.com/acme/app/issues/12",
            "labels": [{"name": "bug"}, {"name": "priority: high"}],
        },
        repository={"full_name": "acme/app", "name": "app"},
    )
    result = await issue_handler.handle_issue_labels_changed(payload, db_session)

    assert result["ok"] is True
    assert captured and captured[0][0] == "task-12"
    assert captured[0][1].task_type == "Bug"
    assert captured[0][1].priority == "High"
    assert captured[0][1].clear_engineer_assignee is True

    payload.issue.labels = [{"name": "bug"}, {"name": "Low-priority"}]
    await issue_handler.handle_issue_labels_changed(payload, db_session)
    assert captured[-1][1].priority == "Low"


@pytest.mark.asyncio
async def test_replayed_issue_open_repairs_existing_task_priority(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="app", github_issue_number=14, plaky_task_id="task-14"))
    await db_session.flush()
    captured: list[Any] = []

    async def fake_update(task_id: str, req: Any) -> dict[str, Any]:
        captured.append((task_id, req))
        return {"ok": True}

    async def fake_routing(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(issue_handler, "get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)

    payload = IssueEventPayload(
        action="opened",
        issue={
            "number": 14,
            "title": "Priority changed",
            "html_url": "https://github.com/acme/app/issues/14",
            "labels": [{"name": "priority: urgent"}],
        },
        repository={"full_name": "acme/app", "name": "app"},
    )
    result = await issue_handler.handle_issue_opened(payload, db_session)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert captured and captured[0][1].priority == "Very Important"


@pytest.mark.asyncio
async def test_issue_edit_syncs_title_and_body(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="app", github_issue_number=15, plaky_task_id="task-15"))
    await db_session.flush()
    captured: list[Any] = []

    async def fake_routing(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(plaky_board_id="board-1", plaky_group_id="group-1")

    async def fake_status(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_update(task_id: str, req: Any) -> dict[str, Any]:
        captured.append((task_id, req))
        return {"ok": True}

    monkeypatch.setattr(issue_handler, "get_routing_async", fake_routing)
    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_status
    )
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)

    payload = IssueEventPayload(
        action="edited",
        issue={
            "number": 15,
            "title": "Updated title",
            "body": "Updated issue details",
            "html_url": "https://github.com/acme/app/issues/15",
            "labels": [],
        },
        repository={"full_name": "acme/app", "name": "app"},
    )

    result = await issue_handler.handle_issue_edited(payload, db_session)

    assert result["ok"] is True
    assert captured and captured[0][0] == "task-15"
    request = captured[0][1]
    assert request.title == "[app] Updated title"
    assert "Updated issue details" in (request.description or "")
    assert request.plaky_board_id == "board-1"
