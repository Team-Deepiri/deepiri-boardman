"""GitHub issue webhooks against a fake in-memory ClickUp (TASK_PROVIDER=clickup)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.clickup.client import ClickUpClient
from boardman.database.models import Base, IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.repos_config import RepoRouting
from boardman.services import clickup_issue_sync as sync
from boardman.services import issue_handler as ih

PEOPLE = {"alice": "12", "bob": "13"}


class FakeClickUp:
    """Just enough of ClickUp's v2 API for the issue flow, with real state."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.log: list[tuple[str, str, dict]] = []
        self.fail_get = False
        self.fail_create = False
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        path = req.url.path.removeprefix("/api/v2")
        self.log.append((req.method, path, body))
        parts = path.strip("/").split("/")
        if req.method == "POST" and parts[0] == "list" and parts[2] == "task":
            if self.fail_create:
                return httpx.Response(500, text="boom")
            self.n += 1
            tid = f"t{self.n}"
            prio = body.get("priority")
            self.tasks[tid] = {
                "id": tid,
                "name": body["name"],
                "description": body.get("description", ""),
                "status": {"status": body.get("status") or "to do"},
                "priority": {"id": prio, "priority": str(prio)} if prio else None,
                "assignees": [{"id": a} for a in body.get("assignees", [])],
                "tags": [{"name": t} for t in body.get("tags", [])],
                "url": f"https://cu/{tid}",
            }
            return httpx.Response(200, json=self.tasks[tid])
        if parts[0] == "task":
            tid = parts[1]
            task = self.tasks.get(tid)
            if task is None:
                return httpx.Response(404, text="nope")
            if len(parts) == 2 and req.method == "GET":
                if self.fail_get:
                    return httpx.Response(500, text="down")
                return httpx.Response(200, json=task)
            if len(parts) == 2 and req.method == "PUT":
                if "name" in body:
                    task["name"] = body["name"]
                if "description" in body:
                    task["description"] = body["description"]
                if "status" in body:
                    task["status"] = {"status": body["status"]}
                if "priority" in body:
                    task["priority"] = {
                        "id": body["priority"],
                        "priority": str(body["priority"]),
                    }
                if "assignees" in body:
                    ids = {str(a["id"]) for a in task["assignees"]}
                    ids |= {str(i) for i in body["assignees"]["add"]}
                    ids -= {str(i) for i in body["assignees"]["rem"]}
                    task["assignees"] = [{"id": i} for i in sorted(ids)]
                return httpx.Response(200, json=task)
            if len(parts) == 3 and parts[2] == "comment":
                task.setdefault("comments", []).append(body["comment_text"])
                return httpx.Response(200, json={"id": len(task["comments"])})
            if len(parts) == 4 and parts[2] == "tag":
                name = parts[3]
                if req.method == "POST":
                    if name not in [t["name"] for t in task["tags"]]:
                        task["tags"].append({"name": name})
                else:
                    task["tags"] = [t for t in task["tags"] if t["name"] != name]
                return httpx.Response(200, json={})
        return httpx.Response(404, text="unhandled")

    def client(self) -> ClickUpClient:
        return ClickUpClient(
            "tok", "https://cu.test/api/v2", transport=httpx.MockTransport(self.handler)
        )

    def writes(self, method: str) -> list[dict]:
        return [b for m, _, b in self.log if m == method]


@pytest.fixture
def cu(monkeypatch):
    fake = FakeClickUp()
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "")

    async def routing(full_name, repo_name, org):
        return RepoRouting(category="devtools", clickup_list_id="L9")

    async def engineer(login: str) -> str:
        return PEOPLE.get((login or "").lower(), "")

    monkeypatch.setattr(sync, "get_routing_async", routing)
    monkeypatch.setattr(sync, "_resolve_engineer", engineer)
    for name, value in {
        "clickup_status_needs_assigned": "to do",
        "clickup_status_assigned": "assigned",
        "clickup_status_in_progress": "in progress",
        "clickup_status_needs_qa": "needs qa",
        "clickup_status_completed": "complete",
    }.items():
        monkeypatch.setattr(sync.settings, name, value)
    return fake


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _issue(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 7,
        "title": "Slow dashboard",
        "body": "it takes ages",
        "html_url": "https://github.com/org/repo/issues/7",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    base.update(over)
    return base


def _payload(action: str, issue: dict[str, Any] | None = None, **extra: Any) -> IssueEventPayload:
    return IssueEventPayload(
        action=action,
        issue=issue or _issue(),
        repository={"full_name": "org/repo", "name": "repo"},
        **extra,
    )


async def _mapping(db) -> IssueTaskMap:
    return (await db.execute(select(IssueTaskMap))).scalar_one()


# -- opened ------------------------------------------------------------------------------------


async def test_opened_creates_a_task_in_the_routed_list(cu, db):
    issue = _issue(assignees=[{"login": "alice"}], labels=[{"name": "bug"}])
    r = await sync.handle_issue_opened(_payload("opened", issue), db, client=cu.client())
    assert r["ok"] and r["plaky_task_id"] == "t1"
    post = next(b for m, p, b in cu.log if m == "POST" and p == "/list/L9/task")
    assert post["name"] == "[repo] Slow dashboard"
    assert post["assignees"] == [12] and post["status"] == "assigned"
    assert "repo" in post["tags"] and "qa" not in json.dumps(post).lower()
    assert "Issue: #7" in post["description"] and "Category: devtools" in post["description"]
    m = await _mapping(db)
    assert (m.plaky_task_id, m.plaky_task_url) == ("t1", "https://cu/t1")
    assert (await db.execute(select(SyncLog).where(SyncLog.action == "issue_created"))).scalar_one()


async def test_unowned_issue_is_needs_assigned_with_nobody_on_it(cu, db):
    await sync.handle_issue_opened(_payload("opened"), db, client=cu.client())
    post = next(b for m, p, b in cu.log if m == "POST")
    assert post["status"] == "to do" and "assignees" not in post


async def test_an_ineligible_owner_never_yields_an_assigned_status(cu, db, monkeypatch):
    async def nobody(login: str) -> str:
        return ""  # the GitHub assignee did not resolve to an eligible developer

    monkeypatch.setattr(sync, "_resolve_engineer", nobody)
    await sync.handle_issue_opened(
        _payload("opened", _issue(assignees=[{"login": "ghost"}])), db, client=cu.client()
    )
    post = next(b for m, p, b in cu.log if m == "POST")
    assert post["status"] == "to do" and "assignees" not in post


async def test_replayed_opened_reconciles_instead_of_creating_again(cu, db):
    await sync.handle_issue_opened(_payload("opened"), db, client=cu.client())
    r = await sync.handle_issue_opened(_payload("opened"), db, client=cu.client())
    assert r["skipped"] is True and "reconciled" in r["message"]
    assert len([1 for m, p, _ in cu.log if m == "POST" and p.endswith("/task")]) == 1


async def test_opened_without_a_list_fails_cleanly_and_leaves_no_reservation(cu, db, monkeypatch):
    async def no_list(*a, **k):
        return RepoRouting(category="x")

    monkeypatch.setattr(sync, "get_routing_async", no_list)
    r = await sync.handle_issue_opened(_payload("opened"), db, client=cu.client())
    assert r["ok"] is False and "ClickUp list" in r["message"]
    await db.flush()
    assert (await db.execute(select(IssueTaskMap))).scalars().all() == []


async def test_a_failed_create_releases_the_reservation_so_a_retry_can_run(cu, db):
    cu.fail_create = True
    r = await sync.handle_issue_opened(_payload("opened"), db, client=cu.client())
    assert r["ok"] is False
    await db.flush()
    assert (await db.execute(select(IssueTaskMap))).scalars().all() == []
    cu.fail_create = False
    assert (await sync.handle_issue_opened(_payload("opened"), db, client=cu.client()))["ok"]


# -- changed -----------------------------------------------------------------------------------


async def _opened(cu, db, **issue_over):
    await sync.handle_issue_opened(_payload("opened", _issue(**issue_over)), db, client=cu.client())
    return cu.tasks["t1"]


async def test_edit_renames_the_task_in_place(cu, db):
    task = await _opened(cu, db)
    r = await sync.handle_issue_changed(
        _payload("edited", _issue(title="Fast dashboard", body="now quick")),
        db,
        event_label="issue_edited_synced",
        client=cu.client(),
    )
    assert r["ok"] and task["name"] == "[repo] Fast dashboard"
    assert "now quick" in task["description"]


async def test_edit_with_no_real_change_writes_nothing(cu, db):
    await _opened(cu, db)
    before = len(cu.writes("PUT"))
    await sync.handle_issue_changed(
        _payload("edited"), db, event_label="issue_edited_synced", client=cu.client()
    )
    assert len(cu.writes("PUT")) == before


async def test_assigned_event_sets_owner_and_status(cu, db):
    task = await _opened(cu, db)
    r = await sync.handle_issue_changed(
        _payload("assigned", _issue(assignees=[{"login": "alice"}])), db, client=cu.client()
    )
    assert r["ok"] and r["status"] == "assigned"
    assert task["status"]["status"] == "assigned"
    assert [a["id"] for a in task["assignees"]] == ["12"]


async def test_assigned_never_moves_work_that_has_already_started_backwards(cu, db):
    task = await _opened(cu, db)
    task["status"] = {"status": "needs qa"}  # the PR flow already moved it on
    r = await sync.handle_issue_changed(
        _payload("assigned", _issue(assignees=[{"login": "bob"}])), db, client=cu.client()
    )
    assert task["status"]["status"] == "needs qa"
    assert r["status_held_back"] == "needs qa"
    assert [a["id"] for a in task["assignees"]] == ["13"]  # the owner still lands


async def test_unreadable_task_applies_none_of_an_ownership_event(cu, db):
    task = await _opened(cu, db)
    cu.fail_get = True
    await sync.handle_issue_changed(
        _payload("assigned", _issue(assignees=[{"login": "alice"}])), db, client=cu.client()
    )
    assert task["status"]["status"] == "to do" and task["assignees"] == []


async def test_label_event_never_overwrites_an_existing_owner(cu, db):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    task["assignees"] = [{"id": "99"}]  # a lead reassigned by hand
    await sync.handle_issue_changed(
        _payload("labeled", _issue(assignees=[{"login": "alice"}])),
        db,
        event_label="issue_labels_synced",
        client=cu.client(),
    )
    assert [a["id"] for a in task["assignees"]] == ["99"]


async def test_label_event_does_not_move_status(cu, db):
    task = await _opened(cu, db)
    task["status"] = {"status": "in progress"}
    await sync.handle_issue_changed(
        _payload("labeled"), db, event_label="issue_labels_synced", client=cu.client()
    )
    assert task["status"]["status"] == "in progress"


async def test_unassigned_removes_only_the_person_who_was_removed(cu, db):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    task["assignees"] = [{"id": "12"}, {"id": "55"}]  # alice plus a QA reviewer
    await sync.handle_issue_changed(
        _payload("unassigned", _issue(), assignee={"login": "alice"}), db, client=cu.client()
    )
    assert [a["id"] for a in task["assignees"]] == ["55"]
    assert task["status"]["status"] == "to do"  # nobody owns it now


async def test_type_tag_follows_the_labels_and_keeps_exactly_one(cu, db):
    task = await _opened(cu, db, labels=[{"name": "bug"}])
    assert "type:bug" in [t["name"] for t in task["tags"]]
    await sync.handle_issue_changed(
        _payload("labeled", _issue(labels=[{"name": "enhancement"}])),
        db,
        event_label="issue_labels_synced",
        client=cu.client(),
    )
    types = [t["name"] for t in task["tags"] if t["name"].startswith("type:")]
    assert len(types) == 1 and types[0] != "type:bug"


async def test_priority_only_follows_github_when_a_human_set_it(cu, db):
    task = await _opened(cu, db)
    task["priority"] = {"id": 4, "priority": "low"}  # a lead's hand-tuned value
    await sync.handle_issue_changed(
        _payload("edited"), db, event_label="issue_edited_synced", client=cu.client()
    )
    assert task["priority"]["id"] == 4
    await sync.handle_issue_changed(
        _payload("edited", _issue(labels=[{"name": "priority: high"}])),
        db,
        event_label="issue_edited_synced",
        client=cu.client(),
    )
    assert task["priority"]["id"] == 2


async def test_changed_for_an_unmapped_issue_is_skipped(cu, db):
    r = await sync.handle_issue_changed(_payload("edited"), db, client=cu.client())
    assert r["skipped"] is True


# -- closed / reopened ---------------------------------------------------------------------------


async def test_close_completes_the_task_and_comments_once(cu, db):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    closed = _payload("closed", _issue(state="closed", assignees=[{"login": "alice"}]))
    await sync.handle_issue_closed(closed, db, client=cu.client())
    await sync.handle_issue_closed(closed, db, client=cu.client())  # redelivery
    assert task["status"]["status"] == "complete"
    assert len(task["comments"]) == 1 and "closed on GitHub" in task["comments"][0]


async def test_reopen_of_an_owned_issue_resumes_the_status_it_held(cu, db):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    task["status"] = {"status": "needs qa"}
    owned = _issue(assignees=[{"login": "alice"}])
    await sync.handle_issue_closed(
        _payload("closed", dict(owned, state="closed")), db, client=cu.client()
    )
    assert task["status"]["status"] == "complete"
    await sync.handle_issue_reopened(_payload("reopened", owned), db, client=cu.client())
    assert task["status"]["status"] == "needs qa"


async def test_reopen_of_an_unowned_issue_is_needs_assigned_never_a_working_status(cu, db):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    task["status"] = {"status": "in progress"}
    await sync.handle_issue_closed(
        _payload("closed", _issue(state="closed")), db, client=cu.client()
    )
    await sync.handle_issue_reopened(_payload("reopened", _issue()), db, client=cu.client())
    assert task["status"]["status"] == "to do"


async def test_reopen_falls_back_when_the_remembered_status_no_longer_exists(cu, db, monkeypatch):
    task = await _opened(cu, db, assignees=[{"login": "alice"}])
    task["status"] = {"status": "needs qa"}
    owned = _issue(assignees=[{"login": "alice"}])
    await sync.handle_issue_closed(
        _payload("closed", dict(owned, state="closed")), db, client=cu.client()
    )

    real = cu.handler

    def picky(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and b"needs qa" in req.content:
            return httpx.Response(400, text="Status not found")
        return real(req)

    c = ClickUpClient("tok", "https://cu.test/api/v2", transport=httpx.MockTransport(picky))
    await sync.handle_issue_reopened(_payload("reopened", owned), db, client=c)
    assert task["status"]["status"] == "assigned"


async def test_close_without_a_configured_status_is_skipped_not_guessed(cu, db, monkeypatch):
    task = await _opened(cu, db)
    monkeypatch.setattr(sync.settings, "clickup_status_completed", "")
    r = await sync.handle_issue_closed(
        _payload("closed", _issue(state="closed")), db, client=cu.client()
    )
    assert r["skipped"] is True and task["status"]["status"] == "to do"


# -- dispatch ------------------------------------------------------------------------------------


async def test_issue_handler_dispatches_to_clickup_only_when_selected(cu, db, monkeypatch):
    from boardman import task_provider

    called: list[str] = []

    async def fake(payload, session, **kw):
        called.append("clickup")
        return {"ok": True}

    monkeypatch.setattr(sync, "handle_issue_opened", fake)
    monkeypatch.setattr(sync, "handle_issue_changed", fake)
    monkeypatch.setattr(sync, "handle_issue_closed", fake)
    monkeypatch.setattr(sync, "handle_issue_reopened", fake)
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    for fn in (
        ih.handle_issue_opened,
        ih.handle_issue_changed,
        ih.handle_issue_closed,
        ih.handle_issue_reopened,
        ih.handle_issue_edited,
        ih.handle_issue_labels_changed,
    ):
        assert (await fn(_payload("opened"), db))["ok"] is True
    assert len(called) == 6
