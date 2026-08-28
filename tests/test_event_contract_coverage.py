"""Every GitHub event in the product contract must reach a handler.

The contract lists the GitHub changes that have to propagate to Plaky. Whether each one
does the *right* thing is what the rest of the suite is for; this file answers the cheaper
question that nothing else asks: does the event reach a handler at all, or does the
dispatch quietly fall through to "Event ignored"?

That failure mode is invisible in production. A GitHub action nobody wired up produces no
error, no log line, and no board change -- it looks exactly like an event that had nothing
to do. Naming every contracted action here means adding one to the contract without wiring
it up fails a test instead of being noticed weeks later on a board that never updated.
"""

from __future__ import annotations

from typing import Any

import pytest

from boardman.routes.github_events import dispatch_github_event

FULL = "Team-Deepiri/deepiri-boardman"
REPO = {"full_name": FULL, "name": "deepiri-boardman"}

# (event_type, action) for every change the contract requires us to propagate.
ISSUE_ACTIONS = [
    "opened",
    "edited",
    "assigned",
    "unassigned",
    "labeled",
    "unlabeled",
    "typed",
    "untyped",
    "closed",
    "reopened",
]
PR_ACTIONS = [
    "opened",
    "edited",
    "assigned",
    "unassigned",
    "labeled",
    "unlabeled",
    "ready_for_review",
    "converted_to_draft",
    "review_requested",
    "review_request_removed",
    "synchronize",
    "reopened",
    "closed",
]
REVIEW_ACTIONS = ["submitted", "dismissed"]
COMMENT_ACTIONS = ["created", "edited"]


def _issue(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": 94,
            "title": "T",
            "body": "b",
            "state": "closed" if action == "closed" else "open",
            "html_url": f"https://github.com/{FULL}/issues/94",
            "labels": [],
            "assignees": [],
        },
        "repository": REPO,
    }


def _pr(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "number": 88,
            "title": "T",
            "body": "b",
            "html_url": f"https://github.com/{FULL}/pull/88",
            "state": "closed" if action == "closed" else "open",
            "merged": False,
            "draft": action == "converted_to_draft",
            "user": {"login": "ali-ferris"},
            "head": {"ref": "feat/x"},
            "labels": [],
        },
        "repository": REPO,
    }


def _review(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "review": {"state": "approved", "user": {"login": "qa-person"}, "id": 5},
        "pull_request": {
            "number": 88,
            "title": "T",
            "body": "b",
            "html_url": f"https://github.com/{FULL}/pull/88",
            "state": "open",
            "merged": False,
            "user": {"login": "ali-ferris"},
        },
        "repository": REPO,
    }


def _review_comment(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "comment": {
            "id": 7,
            "body": "please add a retry",
            "user": {"login": "qa-person"},
            "html_url": f"https://github.com/{FULL}/pull/88#discussion_r7",
        },
        "pull_request": {
            "number": 88,
            "title": "T",
            "body": "b",
            "html_url": f"https://github.com/{FULL}/pull/88",
            "state": "open",
            "merged": False,
            "user": {"login": "ali-ferris"},
        },
        "repository": REPO,
    }


def _issue_comment(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": 88,
            "title": "T",
            "pull_request": {"url": f"https://api.github.com/repos/{FULL}/pulls/88"},
        },
        "comment": {
            "id": 9,
            "body": "a comment",
            "user": {"login": "ali-ferris"},
            "html_url": f"https://github.com/{FULL}/pull/88#issuecomment-9",
        },
        "repository": REPO,
    }


CASES: list[tuple[str, str, dict[str, Any]]] = (
    [("issues", a, _issue(a)) for a in ISSUE_ACTIONS]
    + [("pull_request", a, _pr(a)) for a in PR_ACTIONS]
    + [("pull_request_review", a, _review(a)) for a in REVIEW_ACTIONS]
    + [("pull_request_review_comment", a, _review_comment(a)) for a in COMMENT_ACTIONS]
    + [("issue_comment", a, _issue_comment(a)) for a in COMMENT_ACTIONS]
)


@pytest.fixture()
async def db_session():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from boardman.database.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture()
def offline(monkeypatch):
    """Nothing is mapped and nothing is reachable, so every handler takes its skip path.

    The question here is routing, not behaviour: a handler that says "no Plaky task
    mapped for this issue" was reached, which is all this file asserts.
    """

    class Routing:
        plaky_board_id = ""
        plaky_group_id = ""
        plaky_table = ""
        category = ""

    async def fake_routing(*_a, **_k):
        return Routing()

    for target in (
        "boardman.repos_config.get_routing_async",
        "boardman.services.pr_review_handler.get_routing_async",
        "boardman.services.issue_handler.get_routing_async",
    ):
        monkeypatch.setattr(target, fake_routing, raising=False)
    monkeypatch.setattr(
        "boardman.github.change_signal.note_repo_changed", lambda *_a, **_k: None, raising=False
    )
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "action", "payload"),
    CASES,
    ids=[f"{e}.{a}" for e, a, _ in CASES],
)
async def test_every_contracted_event_reaches_a_handler(
    db_session, offline, event_type: str, action: str, payload: dict[str, Any]
) -> None:
    result = await dispatch_github_event(event_type, payload, db_session)

    assert result.get("message") != "Event ignored", (
        f"{event_type}.{action} falls through the dispatch: it would produce no error, "
        "no log line and no board change in production"
    )


@pytest.mark.asyncio
async def test_an_action_nobody_contracted_is_still_ignored(db_session, offline) -> None:
    """The assertion above must be able to fail, so the opposite case is pinned too."""
    result = await dispatch_github_event("issues", _issue("pinned"), db_session)

    assert result.get("message") == "Event ignored"


@pytest.mark.asyncio
async def test_a_merged_pr_routes_to_the_merge_handler(db_session, offline, monkeypatch) -> None:
    """`closed` splits on merged, so both sides need naming."""
    from boardman.services import pr_handler as ph

    seen: list[str] = []

    async def fake_merged(payload, session):
        seen.append("merged")
        return {"ok": True}

    async def fake_closed(payload, session):
        seen.append("closed_without_merge")
        return {"ok": True}

    monkeypatch.setattr("boardman.routes.github_events.handle_pr_merged", fake_merged)
    monkeypatch.setattr("boardman.routes.github_events.handle_pr_closed_without_merge", fake_closed)
    assert ph  # imported for the reader: these are its handlers

    merged = _pr("closed")
    merged["pull_request"]["merged"] = True
    await dispatch_github_event("pull_request", merged, db_session)
    await dispatch_github_event("pull_request", _pr("closed"), db_session)

    assert seen == ["merged", "closed_without_merge"]
