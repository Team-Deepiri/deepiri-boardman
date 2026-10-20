"""One task traced through the whole GitHub lifecycle, over the real HTTP surface.

The unit tests call handlers directly. This drives the FastAPI app itself: HMAC-signed GitHub
webhooks into ``/api/v1/webhooks/github`` and the UI's ``POST /api/v1/tasks``, with ClickUp
replaced by the in-memory fake. It proves the wiring (signature check, dispatch, provider
selection, handlers, ClickUp writes) and records the ClickUp status after every step.

It does NOT prove ClickUp's real API behaves like the fake. ``scripts/clickup_live_trace.py``
runs the same trace against a real workspace; see docs/CLICKUP_LIVE_CHECKLIST.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.database.session import get_db
from boardman.main import create_app
from boardman.repos_config import RepoRouting
from boardman.services import clickup_issue_sync as isync
from boardman.services import clickup_review_sync as rsync
from boardman.settings import settings
from tests.clickup_world import DEV, FULL, QA, REPO, _world  # noqa: F401  (fixture by name)

SECRET = "trace-secret"
HOOK = "/api/v1/webhooks/github"


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture(name="http")
async def _http(world, monkeypatch):
    """The real app on an in-memory database, with every outside call stubbed at the edge."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "github_webhook_async_enabled", False)

    # Issue sync: route the repo to a ClickUp list and resolve the assignee.
    async def routing(full_name, repo_name, org):
        return RepoRouting(category="devtools", clickup_list_id="L1")

    async def engineer(login: str) -> str:
        return DEV if (login or "").lower() == "dev-ann" else ""

    monkeypatch.setattr(isync, "get_routing_async", routing)
    monkeypatch.setattr(isync, "_resolve_engineer", engineer)
    monkeypatch.setattr(isync.settings, "clickup_default_list_id", "")

    # Review sync: who the GitHub-side facts say.
    async def no_failing(full, pr):
        return []

    async def author(full, pr):
        return "dev-ann"

    async def merged(full, pr):
        return False

    async def commits(full, pr):
        return 1

    monkeypatch.setattr(rsync, "ClickUpClient", type(world.client))
    # Every module that bound the class by name at import time must see the fake, whatever
    # order the suite imported them in.
    from boardman.services import clickup_mutations, clickup_task_ops

    for module in (isync, clickup_mutations, clickup_task_ops):
        monkeypatch.setattr(module, "ClickUpClient", type(world.client))
    monkeypatch.setattr(rsync, "failing_required_checks", no_failing)
    monkeypatch.setattr(rsync, "pr_author_login", author)
    monkeypatch.setattr(rsync, "pr_is_merged", merged)
    monkeypatch.setattr(rsync, "current_commit_count", commits)
    monkeypatch.setattr(rsync, "support_team_logins_casefold", lambda: set())
    monkeypatch.setattr(
        rsync,
        "load_team_assignments",
        lambda: SimpleNamespace(
            members=[
                SimpleNamespace(id=DEV, github_login="dev-ann", display="Ann", roles=["dev"]),
                SimpleNamespace(id=QA, github_login="qa-quinn", display="Quinn", roles=["qa"]),
            ],
            fallback_members=[],
        ),
    )

    app = create_app()
    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


async def _deliver(http: AsyncClient, event: str, payload: dict[str, Any], delivery: str):
    raw = json.dumps(payload).encode()
    return await http.post(
        HOOK,
        content=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sign(raw),
            "Content-Type": "application/json",
        },
    )


def _repo() -> dict[str, str]:
    return {"full_name": FULL, "name": REPO}


def _pr_payload(action: str, *, merged: bool = False, state: str = "open") -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "number": 9,
            "title": "Fix slow dashboard",
            "body": "Fixes #7",
            "html_url": f"https://github.com/{FULL}/pull/9",
            "state": state,
            "merged": merged,
            "draft": False,
            "user": {"login": "dev-ann"},
            "head": {"ref": "feat/x"},
            "base": {"ref": "main"},
            "labels": [],
            "commits": 1,
            "updated_at": "2026-10-04T10:00:00Z",
        },
        "repository": _repo(),
    }


def _review_payload(state: str, login: str, rid: int) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {
            "id": rid,
            "user": {"login": login},
            "state": state,
            "body": "",
            "html_url": f"https://github.com/{FULL}/pull/9#pullrequestreview-{rid}",
            "submitted_at": "2026-10-04T10:00:00Z",
        },
        "pull_request": {"number": 9, "title": "Fix slow dashboard", "body": "Fixes #7"},
        "repository": _repo(),
    }


async def test_a_task_traced_from_issue_to_merged_pr(http, world):
    trace: list[tuple[str, str]] = []

    def status() -> str:
        (task,) = world.fake.tasks.values()
        return task["status"]["status"]

    # 1. An issue is opened and assigned: Boardman creates the ClickUp task.
    r = await _deliver(
        http,
        "issues",
        {
            "action": "opened",
            "issue": {
                "number": 7,
                "title": "Slow dashboard",
                "body": "takes ages",
                "html_url": f"https://github.com/{FULL}/issues/7",
                "state": "open",
                "labels": [],
                "assignees": [{"login": "dev-ann"}],
            },
            "repository": _repo(),
        },
        "d-issue",
    )
    assert r.status_code == 200, r.text
    assert len(world.fake.tasks) == 1, "the issue must create exactly one ClickUp task"
    trace.append(("issue opened + assigned", status()))
    assert status() == "assigned"

    # 2. A PR that says "Fixes #7" opens: linked, QA picked, review requested.
    r = await _deliver(http, "pull_request", _pr_payload("opened"), "d-pr-open")
    assert r.status_code == 200, r.text
    trace.append(("PR opened", status()))
    assert status() == "needs qa"

    # 3. The assigned QA starts reviewing.
    r = await _deliver(
        http, "pull_request_review", _review_payload("commented", "qa-quinn", 1), "d-rv1"
    )
    assert r.status_code == 200, r.text
    trace.append(("QA comments", status()))
    assert status() == "in qa"

    # 4. QA approves.
    r = await _deliver(
        http, "pull_request_review", _review_payload("approved", "qa-quinn", 2), "d-rv2"
    )
    assert r.status_code == 200, r.text
    trace.append(("QA approves", status()))
    assert status() == "qa verified"

    # 5. The PR merges: the task completes.
    r = await _deliver(
        http, "pull_request", _pr_payload("closed", merged=True, state="closed"), "d-pr-merge"
    )
    assert r.status_code == 200, r.text
    trace.append(("PR merged", status()))
    assert status() == "complete"

    # A replayed delivery changes nothing.
    again = await _deliver(
        http, "pull_request", _pr_payload("closed", merged=True, state="closed"), "d-pr-merge"
    )
    assert again.status_code == 200 and status() == "complete"
    assert len(world.fake.tasks) == 1

    print("\nClickUp status trace (task from issue #7 / PR #9):")
    for step, st in trace:
        print(f"  {step:<26} -> {st}")


async def test_a_forged_webhook_never_touches_clickup(http, world):
    raw = json.dumps(_pr_payload("opened")).encode()
    r = await http.post(
        HOOK,
        content=raw,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-forged",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert r.status_code == 401
    assert world.fake.log == []


async def test_the_ui_create_route_makes_a_clickup_task(http, world, monkeypatch):
    """What the 'create task' form in boardman-ui sends."""
    monkeypatch.setattr(settings, "clickup_default_list_id", "L1")
    # Creating a task needs the API token (security PR #175); the UI sends it from sessionStorage.
    monkeypatch.setattr(settings, "boardman_api_token", "tok-123", raising=False)
    anonymous = await http.post("/api/v1/tasks", json={"title": "nope", "plaky_board_id": "L1"})
    assert anonymous.status_code in (401, 404) and not world.fake.tasks
    r = await http.post(
        "/api/v1/tasks",
        headers={"Authorization": "Bearer tok-123"},
        json={
            "title": "Created from the UI",
            "description": "via boardman-ui",
            "priority": "Medium",
            "plaky_board_id": "L1",
            "auto_assign_team": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok"), body
    assert [t["name"] for t in world.fake.tasks.values()] == ["Created from the UI"]
    assert body.get("task_url") or (body.get("task") or {}).get("url")
