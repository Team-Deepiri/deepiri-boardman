"""Shared fixtures and builders for the ClickUp webhook-sync tests: a fake ClickUp, an in-memory
database, GitHub stand-ins and PR payloads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman import task_provider
from boardman.clickup.client import ClickUpClient
from boardman.database.models import Base, IssueTaskMap
from boardman.github.webhooks import PullRequestEventPayload
from boardman.services import clickup_pr_sync as sync
from boardman.services import pr_handler as ph
from tests.clickup_fake import FakeClickUp

REPO = "repo"
FULL = "org/repo"
DEV = "12"
QA = "55"
STATUSES = {
    "needs_assigned": "to do",
    "assigned": "assigned",
    "in_progress": "in progress",
    "needs_qa": "needs qa",
    "in_qa": "in qa",
    "approved": "qa verified",
    "changes_requested": "qa rejected",
    "deployed": "deployed",
    "completed": "complete",
}


@pytest.fixture(name="world")
def _world(monkeypatch):
    fake = FakeClickUp()
    gh: dict[str, list] = {"comments": [], "reviewers": []}
    picks: list[dict[str, Any]] = []

    for name, value in STATUSES.items():
        monkeypatch.setattr(sync.settings, f"clickup_status_{name}", value)
    monkeypatch.setattr(sync.settings, "clickup_qa_field_id", "")
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    monkeypatch.setattr(sync.settings, "skip_needs_qa_for_draft", True)
    monkeypatch.setattr(sync.settings, "complete_when_all_prs_merged", True)

    members = [
        SimpleNamespace(id=DEV, github_login="dev-ann", display="Ann Dev", roles=["dev"]),
        SimpleNamespace(id=QA, github_login="qa-quinn", display="Quinn QA", roles=["qa"]),
    ]
    amb = SimpleNamespace(
        enabled=False, assign_qa=True, title_template="Triage: PR #{number} - {repo}"
    )
    monkeypatch.setattr(
        sync,
        "load_team_assignments",
        lambda: SimpleNamespace(members=members, qa_bug_specialist="", ambiguous_pr=amb),
    )

    async def pick(repo_full, cfg=None, *, exclude_login="", qa_workload=None):
        picks.append({"repo": repo_full, "exclude": exclude_login})
        return QA, "best fit"

    async def resolve(gh_user, **kw):
        return DEV if str(gh_user.get("login") or "").lower() == "dev-ann" else None

    async def comment(full, pr, body):
        gh["comments"].append((pr, body))
        return {"ok": True}

    async def reviewers(full, pr, logins):
        gh["reviewers"].append((pr, logins))
        return {"ok": True}

    async def has_comment(full, pr):
        return any(p == pr for p, _ in gh["comments"])

    monkeypatch.setattr("boardman.assignment.qa_picker.pick_qa_for_repo", pick)
    monkeypatch.setattr(
        "boardman.assignment.github_user_resolution.resolve_github_user_to_user_id", resolve
    )
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: (pid, ""),
    )
    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", reviewers)
    monkeypatch.setattr("boardman.github.pr_actions.has_qa_assignment_comment", has_comment)
    monkeypatch.setattr(ph.settings, "pr_task_sync_skip_bot_authors", False, raising=False)
    # The handlers build their own client; route every construction to the fake.
    bound = _bound_client_class(fake)
    monkeypatch.setattr(sync, "ClickUpClient", bound)
    monkeypatch.setattr("boardman.clickup.client.ClickUpClient", bound)
    return SimpleNamespace(fake=fake, gh=gh, picks=picks, client=bound(), amb=amb)


def _bound_client_class(fake: FakeClickUp) -> type[ClickUpClient]:
    """A ClickUpClient that always talks to ``fake``, however and wherever it is constructed."""

    class _Bound(ClickUpClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(
                "tok", "https://cu.test/api/v2", transport=httpx.MockTransport(fake.handler)
            )

    return _Bound


@pytest_asyncio.fixture(name="db")
async def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _pr(
    action: str = "opened",
    *,
    number: int = 9,
    body: str = "Fixes #7",
    title: str = "Fix the thing",
    draft: bool = False,
    state: str = "open",
    labels: list[str] | None = None,
    commits: int = 1,
    merged: bool = False,
    author: str = "dev-ann",
    head_ref: str = "feat/x",
    **extra: Any,
) -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action=action,
        pull_request={
            "number": number,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{FULL}/pull/{number}",
            "state": state,
            "merged": merged,
            "draft": draft,
            "user": {"login": author},
            "head": {"ref": head_ref},
            "base": {"ref": "main"},
            "labels": [{"name": n} for n in (labels or [])],
            "commits": commits,
            "updated_at": "2026-10-04T10:00:00Z",
        },
        repository={"full_name": FULL, "name": REPO},
        **extra,
    )


async def _task(world, db, *, issue: int = 7, status: str = "to do", assignees=()) -> dict:
    """An issue's task, already on the board and mapped."""
    body = {"name": "[repo] Slow", "status": status, "assignees": list(assignees)}
    created = await world.client.create_task(
        body["name"],
        "d",
        "medium",
        board_id="L1",
        status=status,
        assignee_ids=list(assignees) or None,
    )
    tid = created["task_id"]
    db.add(
        IssueTaskMap(
            github_repo=REPO,
            github_issue_number=issue,
            plaky_task_id=tid,
            plaky_task_url=created["task_url"],
        )
    )
    await db.commit()
    return world.fake.tasks[tid]


def _status(task: dict) -> str:
    return task["status"]["status"]


def _ids(task: dict) -> list[str]:
    return sorted(a["id"] for a in task["assignees"])
