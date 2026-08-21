"""Reconciliation repairs drift, and repairing twice changes nothing the second time.

Webhooks are the primary path and reconciliation is the net underneath them: it exists for
the deliveries GitHub never made, the ones that arrived while the process was down, and the
writes that failed halfway. That makes it the one path nobody watches, so the properties it
has to hold are pinned here rather than assumed.

Every case builds a real drift -- an issue with no task, a PR whose link was never made,
stale metadata on a task that exists -- runs the canonical `reconcile_repo`, and asserts
GitHub won, the board was repaired, and a second run is a no-op. GitHub is authoritative
for development state; reconciliation is how Plaky catches up to it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, PullRequestTaskLink
from boardman.services import reconcile as rc
from boardman.settings import settings

FULL = "Team-Deepiri/deepiri-boardman"
REPO = "deepiri-boardman"
TASK_94 = "7209283"


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _issue(number: int, *, state: str = "open", assignee: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "b",
        "state": state,
        "html_url": f"https://github.com/{FULL}/issues/{number}",
        "labels": [],
        "assignees": [{"login": assignee}] if assignee else [],
    }


def _pull(number: int, *, body: str = "", state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "state": state,
        "merged": False,
        "draft": False,
        "html_url": f"https://github.com/{FULL}/pull/{number}",
        "user": {"login": "ali-ferris"},
        "head": {"ref": "feat/x"},
        "labels": [],
        "pull_request": {},
        "updated_at": "2026-08-21T10:00:00Z",
    }


@pytest.fixture()
def github(monkeypatch):
    """A GitHub whose issue and PR lists the test controls."""
    state: dict[str, list[dict[str, Any]]] = {"issues": [], "pulls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(200, json=state["issues"])
        if path.endswith("/pulls"):
            return httpx.Response(200, json=state["pulls"])
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "github_pat", "t")
    monkeypatch.setattr(rc, "github_http_client", lambda: client)
    return state


@pytest.fixture()
def handlers(monkeypatch):
    """Record which repair each drift triggered, without touching Plaky."""
    calls: dict[str, list[Any]] = {"created": [], "issue_synced": [], "pr_opened": []}

    async def fake_issue_opened(payload, session):
        calls["created"].append(payload.issue.number)
        session.add(
            IssueTaskMap(
                github_repo=REPO,
                github_issue_number=payload.issue.number,
                plaky_task_id=TASK_94,
            )
        )
        await session.commit()
        return {"ok": True, "plaky_task_id": TASK_94}

    async def fake_issue_edited(payload, session):
        calls["issue_synced"].append(payload.issue.number)
        return {"ok": True, "event": "issue_labels_synced"}

    async def fake_issue_closed(payload, session):
        calls["issue_synced"].append(payload.issue.number)
        return {"ok": True, "event": "issue_labels_synced"}

    async def fake_pr_opened(payload, session):
        # Stands in for handle_pr_opened, so it writes the link row that one writes.
        calls["pr_opened"].append(payload.pull_request.number)
        session.add(
            PullRequestTaskLink(
                github_repo=REPO,
                github_pr_number=payload.pull_request.number,
                github_issue_number=94,
                plaky_task_id=TASK_94,
                link_source="issue_keyword",
            )
        )
        await session.commit()
        return {"ok": True, "linked": True, "plaky_task_id": TASK_94}

    # The PR branch of reconcile_repo runs the REAL handle_pr_edited, which is the point
    # -- that is the path that repairs a late issue reference. Its Plaky writes are stubbed
    # here so the test is hermetic: conftest loads the live PLAKY_API_KEY into the
    # environment, so an unstubbed run posts comments to the real board.
    from boardman.services import pr_handler as ph

    class FakePlaky:
        async def add_comment(self, *_a, **_k):
            return {"ok": True}

        async def get_board_item_public(self, *_a, **_k):
            return {"ok": False, "item": None}

    async def noop(*_a, **_k):
        return {}

    async def noop_qa(*_a, **_k):
        return {"assigned": False}

    async def noop_ok(*_a, **_k):
        return {"ok": True}

    monkeypatch.setattr(ph, "PlakyClient", lambda *a, **k: FakePlaky())
    monkeypatch.setattr(ph, "_apply_pr_type_and_assignee", noop)
    monkeypatch.setattr(ph, "_assign_qa_for_pr", noop_qa)
    monkeypatch.setattr(ph, "_maybe_set_needs_qa", noop_ok)
    monkeypatch.setattr(ph, "update_task_internal", noop_ok)

    monkeypatch.setattr(rc, "handle_issue_opened", fake_issue_opened)
    monkeypatch.setattr(rc, "handle_pr_opened", fake_pr_opened)
    monkeypatch.setattr(
        "boardman.services.issue_handler.handle_issue_edited", fake_issue_edited, raising=False
    )
    monkeypatch.setattr(
        "boardman.services.issue_handler.handle_issue_closed", fake_issue_closed, raising=False
    )
    return calls


@pytest.mark.asyncio
async def test_an_issue_with_no_task_gets_one(db_session, github, handlers) -> None:
    """The drift reconciliation exists for: a delivery GitHub never made."""
    github["issues"] = [_issue(94)]

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["ok"] is True
    assert out["issues_checked"] == 1
    assert out["tasks_created"] == 1
    assert handlers["created"] == [94]
    mapped = (await db_session.execute(select(IssueTaskMap))).scalars().all()
    assert [m.github_issue_number for m in mapped] == [94]


@pytest.mark.asyncio
async def test_repairing_twice_creates_nothing_the_second_time(
    db_session, github, handlers
) -> None:
    """The property that makes a safety net safe to run on a timer."""
    github["issues"] = [_issue(94)]

    first = await rc.reconcile_repo(FULL, db_session)
    second = await rc.reconcile_repo(FULL, db_session)

    assert first["tasks_created"] == 1
    assert second["tasks_created"] == 0, "the task already exists; do not make another"
    assert handlers["created"] == [94], "handle_issue_opened is not called again"
    mapped = (await db_session.execute(select(IssueTaskMap))).scalars().all()
    assert len(mapped) == 1, "no duplicate mapping"


@pytest.mark.asyncio
async def test_an_issue_that_already_has_a_task_is_resynced_not_recreated(
    db_session, github, handlers
) -> None:
    """Stale metadata on an existing task is repaired through the normal sync path."""
    db_session.add(IssueTaskMap(github_repo=REPO, github_issue_number=94, plaky_task_id=TASK_94))
    await db_session.commit()
    github["issues"] = [_issue(94, assignee="ali-ferris")]

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["tasks_created"] == 0
    assert handlers["created"] == []
    assert handlers["issue_synced"] == [94], "GitHub's current state is pushed to the board"


@pytest.mark.asyncio
async def test_a_closed_issue_nobody_tracked_is_not_filed_as_new_work(
    db_session, github, handlers
) -> None:
    """Otherwise every reconciliation grows the board with work already finished."""
    github["issues"] = [_issue(77, state="closed")]

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["tasks_created"] == 0
    assert handlers["created"] == []
    assert out.get("issues_skipped_closed") == 1


@pytest.mark.asyncio
async def test_a_pr_whose_link_was_never_made_is_relinked(db_session, github, handlers) -> None:
    """The missing-PR-link drift: the PR says Fixes #94 and nothing recorded it."""
    db_session.add(IssueTaskMap(github_repo=REPO, github_issue_number=94, plaky_task_id=TASK_94))
    await db_session.commit()
    github["pulls"] = [_pull(88, body="Fixes #94")]

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["prs_checked"] == 1
    assert out["prs_relinked"] >= 1
    links = (await db_session.execute(select(PullRequestTaskLink))).scalars().all()
    assert [(row.github_issue_number, row.plaky_task_id) for row in links] == [(94, TASK_94)]


@pytest.mark.asyncio
async def test_relinking_a_pr_twice_leaves_one_link(db_session, github, handlers) -> None:
    db_session.add(IssueTaskMap(github_repo=REPO, github_issue_number=94, plaky_task_id=TASK_94))
    await db_session.commit()
    github["pulls"] = [_pull(88, body="Fixes #94")]

    await rc.reconcile_repo(FULL, db_session)
    await rc.reconcile_repo(FULL, db_session)

    links = (await db_session.execute(select(PullRequestTaskLink))).scalars().all()
    live = [row for row in links if row.withdrawn_at is None]
    assert len(live) == 1, "reconciliation must not accumulate links"


@pytest.mark.asyncio
async def test_a_pr_that_gained_its_reference_later_is_repaired_by_reconciliation(
    db_session, github, handlers
) -> None:
    """The webhook fix has a net under it: even a missed `edited` is caught here."""
    db_session.add(IssueTaskMap(github_repo=REPO, github_issue_number=94, plaky_task_id=TASK_94))
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=88,
            github_issue_number=0,
            plaky_task_id="7183844",
            link_source="pr_task_created",
        )
    )
    await db_session.commit()
    github["pulls"] = [_pull(88, body="Fixes #94")]

    await rc.reconcile_repo(FULL, db_session)

    links = {
        row.github_issue_number: row
        for row in (await db_session.execute(select(PullRequestTaskLink))).scalars().all()
    }
    assert links[94].plaky_task_id == TASK_94
    assert links[94].withdrawn_at is None
    assert links[0].withdrawn_at is not None, "the standalone task is superseded here too"


@pytest.mark.asyncio
async def test_one_failing_item_does_not_abandon_the_rest(db_session, github, monkeypatch) -> None:
    """A repo is reconciled as a whole; one bad issue must not stop the others."""
    seen: list[int] = []

    async def fake_issue_opened(payload, session):
        if payload.issue.number == 1:
            raise RuntimeError("this one is broken")
        seen.append(payload.issue.number)
        session.add(
            IssueTaskMap(
                github_repo=REPO,
                github_issue_number=payload.issue.number,
                plaky_task_id=f"t{payload.issue.number}",
            )
        )
        await session.commit()
        return {"ok": True, "plaky_task_id": f"t{payload.issue.number}"}

    monkeypatch.setattr(rc, "handle_issue_opened", fake_issue_opened)
    github["issues"] = [_issue(1), _issue(2), _issue(3)]

    out = await rc.reconcile_repo(FULL, db_session)

    assert seen == [2, 3]
    assert out["ok"] is False, "the failure is reported, not swallowed"
    assert any("issue #1" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_reconciliation_reports_rather_than_raises_when_github_is_down(
    db_session, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"message": "bad gateway"})

    monkeypatch.setattr(settings, "github_pat", "t")
    monkeypatch.setattr(
        rc, "github_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["ok"] is False
    assert "502" in out["message"], out


@pytest.mark.asyncio
async def test_reconciliation_needs_a_token_and_says_so(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_pat", "")

    out = await rc.reconcile_repo(FULL, db_session)

    assert out["ok"] is False
    assert "GITHUB_PAT" in out["message"]
