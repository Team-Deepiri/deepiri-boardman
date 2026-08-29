"""GitHub deployment_status (CD success) -> Plaky Deployed status.

See boardman.services.pr_handler.handle_deployment_status.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, PullRequestTaskLink
from boardman.github.webhooks import DeploymentStatusEventPayload
from boardman.repos_config import RepoRouting
from boardman.services.pr_handler import handle_deployment_status

_NORMALIZED_WITH_DEPLOYED = {
    "board_name": "diri-cyrex",
    "fields": [
        {
            "name": "Status",
            "type": "STATUS",
            "key": "status-6",
            "options": [
                {"name": "In Progress", "id": "2"},
                {"name": "Needs QA", "id": "4"},
                {"name": "Completed", "id": "9"},
                {"name": "Deployed", "id": "10"},
            ],
        }
    ],
}

_NORMALIZED_NO_DEPLOYED = {
    "board_name": "bots",
    "fields": [
        {
            "name": "Status",
            "type": "STATUS",
            "key": "status-6",
            "options": [
                {"name": "In Progress", "id": "2"},
                {"name": "Completed", "id": "9"},
            ],
        }
    ],
}


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class _FakePlaky:
    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    async def patch_item_field_values(self, board_id, item_id, values, **kwargs):
        self.patches.append((item_id, dict(values)))
        return {"ok": True}

    async def get_task(self, task_id):
        return {"ok": True, "task": {"boardId": "269558", "id": task_id}}

    async def update_task_fields(self, task_id, **kwargs):
        return {"ok": True}


def _payload(state: str = "success") -> DeploymentStatusEventPayload:
    return DeploymentStatusEventPayload(
        action="created",
        deployment={"sha": "abc123def456", "ref": "main", "environment": "production"},
        deployment_status={"state": state},
        repository={"full_name": "Team-Deepiri/diri-cyrex", "name": "diri-cyrex"},
    )


def _wire(monkeypatch, fake_plaky, normalized, pr_numbers):
    import boardman.services.task_mutations as tm

    monkeypatch.setattr("boardman.services.pr_handler.PlakyClient", lambda: fake_plaky)
    monkeypatch.setattr(tm, "PlakyClient", lambda: fake_plaky)

    async def _norm(*_a, **_k):
        return normalized

    async def _bundle(*_a, **_k):
        return {"ok": True, "normalized": normalized}

    async def _noop(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status._load_normalized", _norm)
    monkeypatch.setattr(tm, "fetch_board_schema_bundle", _bundle)
    monkeypatch.setattr(tm, "sync_team_assignment_field_keys_from_board", _noop)

    async def _routing(*_a, **_k):
        return RepoRouting(plaky_board_id="269558")

    monkeypatch.setattr("boardman.repos_config.get_routing_async", _routing)

    async def _prs_for_sha(*_a, **_k):
        return pr_numbers

    monkeypatch.setattr("boardman.services.pr_handler._prs_for_commit_sha", _prs_for_sha)


@pytest.mark.asyncio
async def test_deployment_success_moves_task_to_deployed(monkeypatch):
    fake = _FakePlaky()
    _wire(monkeypatch, fake, _NORMALIZED_WITH_DEPLOYED, [55])
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="diri-cyrex",
                github_pr_number=55,
                plaky_task_id="task-x",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()
    async with factory() as session:
        out = await handle_deployment_status(_payload("success"), session)
    assert out["event"] == "deployed"
    assert fake.patches and fake.patches[0][0] == "task-x"
    assert fake.patches[0][1]["status-6"] == "10"
    await engine.dispose()


@pytest.mark.asyncio
async def test_deployment_pending_is_ignored(monkeypatch):
    fake = _FakePlaky()
    _wire(monkeypatch, fake, _NORMALIZED_WITH_DEPLOYED, [55])
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        out = await handle_deployment_status(_payload("pending"), session)
    assert out.get("skipped") is True
    assert fake.patches == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_deployment_success_no_matching_pr_is_skipped(monkeypatch):
    fake = _FakePlaky()
    _wire(monkeypatch, fake, _NORMALIZED_WITH_DEPLOYED, [])  # no PR found for this sha
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        out = await handle_deployment_status(_payload("success"), session)
    assert out.get("skipped") is True
    assert fake.patches == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_deployment_success_board_without_deployed_column_is_skipped(monkeypatch):
    """No dedicated Deployed status on this board — Completed (already set at merge
    time) already represents done here; nothing more for this handler to do."""
    fake = _FakePlaky()
    _wire(monkeypatch, fake, _NORMALIZED_NO_DEPLOYED, [55])
    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="diri-cyrex",
                github_pr_number=55,
                plaky_task_id="task-x",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()
    async with factory() as session:
        out = await handle_deployment_status(_payload("success"), session)
    assert out.get("skipped") is True
    assert fake.patches == []
    await engine.dispose()
