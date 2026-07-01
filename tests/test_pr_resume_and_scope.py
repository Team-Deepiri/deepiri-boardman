"""Resume-after-rejection (synchronize), merge→Completed schema resolution, and the
worker-only production scope flag."""

from __future__ import annotations

from typing import Any, Dict

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import boardman.services.task_mutations as tm
from boardman.database.models import Base, PullRequestTaskLink
from boardman.github.webhooks import GitHubPullRequest, GitHubRepository, PullRequestEventPayload
from boardman.repos_config import RepoRouting
from boardman.services.pr_handler import handle_pr_synchronized
from boardman.settings import settings

# status-6 with non-sequential ids, mirroring the real cyrex board.
_NORMALIZED = {
    "board_name": "diri-cyrex",
    "fields": [
        {
            "name": "Status",
            "type": "STATUS",
            "key": "status-6",
            "options": [
                {"name": "In Progress", "id": "2"},
                {"name": "Needs QA", "id": "4"},
                {"name": "In QA", "id": "5"},
                {"name": "QA Verified", "id": "6"},
                {"name": "QA Rejected", "id": "7"},
                {"name": "Assigned", "id": "8"},
            ],
        }
    ],
}


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class _SyncPlaky:
    def __init__(self, current_status_value: str):
        self.current = current_status_value
        self.patches: list[tuple[str, dict]] = []

    async def get_board_item_public(self, board_id: str, item_id: str) -> Dict[str, Any]:
        return {"ok": True, "item": {"id": item_id, "status-6": self.current}}

    async def patch_item_field_values(self, board_id, item_id, values, **kwargs):
        self.patches.append((item_id, dict(values)))
        return {"ok": True}

    async def get_task(self, task_id):
        return {"ok": True, "task": {"boardId": "269558", "id": task_id}}

    async def update_task_fields(self, task_id, **kwargs):
        return {"ok": True}


def _wire(monkeypatch: pytest.MonkeyPatch, fake: _SyncPlaky) -> None:
    monkeypatch.setattr("boardman.services.pr_handler.PlakyClient", lambda: fake)
    monkeypatch.setattr(tm, "PlakyClient", lambda: fake)

    async def _norm(*_a, **_k):
        return _NORMALIZED

    async def _bundle(*_a, **_k):
        return {"ok": True, "normalized": _NORMALIZED}

    async def _noop(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status._load_normalized", _norm)
    monkeypatch.setattr(tm, "fetch_board_schema_bundle", _bundle)
    monkeypatch.setattr(tm, "sync_team_assignment_field_keys_from_board", _noop)

    async def _routing(*_a, **_k):
        return RepoRouting(plaky_board_id="269558")

    monkeypatch.setattr("boardman.repos_config.get_routing_async", _routing)


def _payload() -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action="synchronize",
        pull_request=GitHubPullRequest(
            number=55, title="t", html_url="http://pr/55", state="open", body=""
        ),
        repository=GitHubRepository(full_name="Team-Deepiri/diri-cyrex", name="diri-cyrex"),
    )


@pytest.mark.asyncio
async def test_synchronize_resumes_in_progress_when_rejected(monkeypatch: pytest.MonkeyPatch):
    fake = _SyncPlaky(current_status_value="7")  # currently QA Rejected
    _wire(monkeypatch, fake)
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="diri-cyrex",
                github_pr_number=55,
                plaky_task_id="task-r",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()
    async with factory() as session:
        out = await handle_pr_synchronized(_payload(), session)
    assert out.get("event") == "resumed_after_rejection"
    # Patched status-6 to In Progress (id "2").
    assert fake.patches and fake.patches[0][1].get("status-6") == "2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_synchronize_noop_when_not_rejected(monkeypatch: pytest.MonkeyPatch):
    fake = _SyncPlaky(current_status_value="5")  # In QA, not rejected
    _wire(monkeypatch, fake)
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="diri-cyrex",
                github_pr_number=55,
                plaky_task_id="task-r",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()
    async with factory() as session:
        out = await handle_pr_synchronized(_payload(), session)
    assert out.get("updated") == []
    assert fake.patches == []
    await engine.dispose()


def test_agent_routes_gated_by_flag(monkeypatch: pytest.MonkeyPatch):
    from boardman.main import create_app

    monkeypatch.setattr(settings, "boardman_enable_agent_api", True)
    paths_on = {getattr(r, "path", "") for r in create_app().routes}
    assert any("/agent/chat" in p for p in paths_on)
    assert any("/webhooks/github" in p for p in paths_on)

    monkeypatch.setattr(settings, "boardman_enable_agent_api", False)
    paths_off = {getattr(r, "path", "") for r in create_app().routes}
    assert not any("/agent/" in p for p in paths_off)
    # Worker-only surface still present:
    assert any("/webhooks/github" in p for p in paths_off)
    assert any("/health" in p for p in paths_off)
    assert any("/assignment/" in p for p in paths_off)
