"""When no existing Plaky task plausibly matches an opened PR, Boardman creates one and
assigns QA instead of leaving the PR untracked — see meeting notes: "check whether there is
already an existing Plaky task relating to it; if not, create one and assign a QA."
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, SyncLog
from boardman.github.webhooks import PullRequestEventPayload
from boardman.services import pr_handler as ph
from boardman.services.pr_task_linking import PipelineResult


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class _FakePlaky:
    created: list[dict] = []
    comments: list[tuple[str, str]] = []

    def __init__(self) -> None:
        pass

    async def create_task(self, *, title, description, board_id, group_id, **kw) -> dict:
        _FakePlaky.created.append(
            {"title": title, "description": description, "board_id": board_id, "group_id": group_id}
        )
        return {"ok": True, "task": {"id": "new-task-1"}}

    async def add_comment(self, task_id: str, body: str, **kw: Any) -> dict:
        _FakePlaky.comments.append((str(task_id), body))
        return {"ok": True}


@pytest.fixture()
def fake_plaky(monkeypatch: pytest.MonkeyPatch) -> type[_FakePlaky]:
    _FakePlaky.created = []
    _FakePlaky.comments = []
    monkeypatch.setattr(ph, "PlakyClient", _FakePlaky)
    return _FakePlaky


def _pr_payload(*, action: str = "opened", number: int = 46) -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action=action,
        pull_request={
            "number": number,
            "title": "QA-end-to-end Happy path",
            "body": "",
            "html_url": f"https://github.com/o/r/pull/{number}",
            "draft": False,
            "user": {"login": "dev1"},
            "head": {"ref": "feature/qa-e2e"},
            "labels": [],
        },
        repository={"full_name": "o/r", "name": "r"},
    )


@pytest.mark.asyncio
async def test_no_plausible_match_creates_and_links_task(db_session, monkeypatch, fake_plaky) -> None:
    async def fake_routing(*a, **k):
        class _R:
            plaky_board_id = "board-1"
            plaky_group_id = "group-1"

        return _R()

    async def fake_pipeline(**kw) -> PipelineResult:
        return PipelineResult(decision="triage", task_id=None, score=10.0, reason="below_medium_threshold")

    async def fake_type_assignee(*a, **k):
        return {}

    async def fake_qa(*a, **k):
        return {"assigned": False}

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr(ph, "run_pr_task_pipeline", fake_pipeline)
    monkeypatch.setattr(ph, "_apply_pr_type_and_assignee", fake_type_assignee)
    monkeypatch.setattr(ph, "_assign_qa_for_pr", fake_qa)
    monkeypatch.setattr(ph, "_maybe_triage_ambiguous_pr", lambda *a, **k: _never_called())

    result = await ph.handle_pr_opened(_pr_payload(), db_session)

    assert result["ok"] is True
    assert result["created"][0]["task_id"] == "new-task-1"
    assert _FakePlaky.created and _FakePlaky.created[0]["title"] == "QA-end-to-end Happy path"
    assert _FakePlaky.created[0]["board_id"] == "board-1"
    assert _FakePlaky.comments, "PR should get a comment pointing at the created task"

    logs = (await db_session.execute(select(SyncLog).where(SyncLog.action == "pr_created_task"))).scalars().all()
    assert len(logs) == 1
    assert logs[0].plaky_task_id == "new-task-1"


async def _never_called():
    raise AssertionError("triage fallback should not run once a task was created")
