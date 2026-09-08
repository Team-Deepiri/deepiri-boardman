"""A finished pull request must never grow a new task.

Live failure (2026-08-19): reconciliation walks `pulls?state=all` so it can complete
merged PRs that still have a link. Unlinked ones fell through to handle_pr_opened,
which manufactures a task, assigns a QA and parks it at Needs QA. The board filled
with tasks named "Merge main into dev" and a dependabot bump merged long ago sitting
in Needs QA with a QA engineer on it — the state Ali reported as out of sync.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.github.webhooks import PullRequestEventPayload
from boardman.services import pr_handler as ph


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _pr_payload(*, state: str = "open", merged: bool = False, number: int = 50) -> Any:
    return PullRequestEventPayload(
        action="opened",
        pull_request={
            "number": number,
            "title": "chore(deps): bump the pip group across 1 directory",
            "body": "",
            "html_url": f"https://github.com/o/r/pull/{number}",
            "state": state,
            "merged": merged,
            "draft": False,
            "user": {"login": "dependabot[bot]"},
            "labels": [],
            "head": {"ref": "dependabot/pip/group"},
        },
        repository={"full_name": "o/r", "name": "r"},
    )


@pytest.mark.asyncio
async def test_merged_pr_gets_no_orphan_task(db_session, monkeypatch) -> None:
    async def boom(*a: Any, **kw: Any):
        raise AssertionError("a merged PR must never reach task creation")

    monkeypatch.setattr("boardman.services.task_mutations.create_task_internal", boom)
    res = await ph._maybe_triage_ambiguous_pr(_pr_payload(state="closed", merged=True), db_session)
    assert res and res.get("skipped") is True
    assert "already closed" in res["message"]


@pytest.mark.asyncio
async def test_closed_unmerged_pr_gets_no_orphan_task(db_session, monkeypatch) -> None:
    async def boom(*a: Any, **kw: Any):
        raise AssertionError("a closed PR must never reach task creation")

    monkeypatch.setattr("boardman.services.task_mutations.create_task_internal", boom)
    res = await ph._maybe_triage_ambiguous_pr(_pr_payload(state="closed"), db_session)
    assert res and res.get("skipped") is True


@pytest.mark.asyncio
async def test_open_pr_still_reaches_triage(db_session, monkeypatch) -> None:
    """The guard must not disable the feature it protects."""
    reached: list[bool] = []

    async def fake_routing(*a: Any, **kw: Any):
        reached.append(True)
        raise RuntimeError("stop after the guard")

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    with pytest.raises(RuntimeError):
        await ph._maybe_triage_ambiguous_pr(_pr_payload(state="open"), db_session)
    assert reached == [True]


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeGitHub:
    """Serves the two list calls reconcile makes: issues, then pulls."""

    def __init__(
        self, pulls: list[dict[str, Any]], issues: list[dict[str, Any]] | None = None
    ) -> None:
        self._pulls = pulls
        self._issues = issues or []

    async def get(self, url: str, **kw: Any) -> _FakeResponse:
        return _FakeResponse(self._pulls if "/pulls" in url else self._issues)


@pytest.mark.asyncio
async def test_reconcile_skips_unlinked_closed_prs(db_session, monkeypatch) -> None:
    """Reconcile still sweeps state=all so it can finish merged work that IS linked,
    but an unlinked closed PR is history: skipped and counted, never re-created."""
    from boardman.services import reconcile as rc

    pulls = [
        {"number": 50, "state": "closed", "merged": True, "title": "bump", "updated_at": "x"},
        {"number": 55, "state": "closed", "merged": False, "title": "abandoned", "updated_at": "x"},
        {"number": 88, "state": "open", "merged": False, "title": "live work", "updated_at": "x"},
    ]

    async def no_links(*a: Any, **kw: Any) -> list[str]:
        return []

    opened: list[int] = []

    async def fake_opened(payload: Any, session: Any, *, is_replay: bool = False) -> dict[str, Any]:
        opened.append(payload.pull_request.number)
        return {"plaky_task_id": "t-new"}

    monkeypatch.setattr(rc, "github_http_client", lambda: _FakeGitHub(pulls))
    monkeypatch.setattr(rc, "distinct_task_ids_for_pr", no_links)
    monkeypatch.setattr(rc, "handle_pr_opened", fake_opened)
    monkeypatch.setattr("boardman.settings.settings.github_pat", "test-token")

    out = await rc.reconcile_repo("o/r", db_session)
    assert opened == [88]  # only the live PR
    assert out["prs_skipped_closed"] == 2
    assert out["prs_relinked"] == 1


@pytest.mark.asyncio
async def test_reconcile_skips_untracked_closed_issues(db_session, monkeypatch) -> None:
    """A closed issue nobody ever tracked must not become a fresh NEEDS ASSIGNED task."""
    from boardman.services import reconcile as rc

    issues = [
        {"number": 10, "state": "closed", "title": "ancient", "html_url": "u"},
        {"number": 11, "state": "open", "title": "live", "html_url": "u"},
    ]

    async def no_mapping(*a: Any, **kw: Any) -> None:
        return None

    created: list[int] = []

    async def fake_open(payload: Any, session: Any) -> dict[str, Any]:
        created.append(payload.issue.number)
        return {"plaky_task_id": "t-new"}

    monkeypatch.setattr(rc, "github_http_client", lambda: _FakeGitHub([], issues))
    monkeypatch.setattr(rc, "find_plaky_task_by_issue", no_mapping)
    monkeypatch.setattr(rc, "handle_issue_opened", fake_open)
    monkeypatch.setattr("boardman.settings.settings.github_pat", "test-token")

    out = await rc.reconcile_repo("o/r", db_session)
    assert created == [11]
    assert out["issues_skipped_closed"] == 1
    assert out["tasks_created"] == 1
