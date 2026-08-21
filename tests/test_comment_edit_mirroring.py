"""An edited GitHub comment must reach Plaky exactly once, and never twice.

Reproduced by hand: `handle_issue_comment_on_pr` opened with

    if payload.action != "created":
        return {"ok": True, "message": "ignored non-created comment"}

so every `issue_comment.edited` was dropped. The board kept showing the original wording
with no sign it had been corrected.

Plaky's API can create a comment and nothing else -- no edit verb, no delete verb -- so
the correction is posted as its own labelled entry. What makes that safe rather than
duplicative is the dedupe key: (comment id, wording). Each distinct wording is mirrored
once no matter how many times GitHub delivers it, and delivery is at-least-once.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, PullRequestTaskLink, SyncLog
from boardman.github.webhooks import IssueCommentEventPayload
from boardman.services import pr_review_handler as rh

REPO = "deepiri-boardman"
FULL = "Team-Deepiri/deepiri-boardman"
TASK = "7209283"
COMMENT_ID = 3344556677


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture()
def posted(monkeypatch):
    """Record every comment body Plaky is asked to create."""
    bodies: list[tuple[str, str]] = []

    class FakePlaky:
        async def add_comment(self, task_id, body, *, board_id=None):
            bodies.append((task_id, body))
            return {"ok": True}

        async def get_board_item_public(self, board_id, item_id):
            # No item detail: the QA-mention path reads current field values from here and
            # correctly does nothing when the board cannot be read.
            return {"ok": False, "item": None}

    class Routing:
        plaky_board_id = "269031"
        plaky_group_id = "g1"

    async def fake_routing(*_a, **_k):
        return Routing()

    monkeypatch.setattr(rh, "PlakyClient", lambda *a, **k: FakePlaky())
    monkeypatch.setattr(rh, "get_routing_async", fake_routing)
    return bodies


def _comment_event(
    action: str,
    body: str,
    *,
    comment_id: int = COMMENT_ID,
    login: str = "ali-ferris",
    is_pr: bool = True,
    number: int = 88,
) -> IssueCommentEventPayload:
    issue: dict = {"number": number, "title": "T"}
    if is_pr:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{FULL}/pulls/{number}"}
    return IssueCommentEventPayload(
        action=action,
        issue=issue,
        comment={
            "id": comment_id,
            "body": body,
            "user": {"login": login},
            "html_url": f"https://github.com/{FULL}/pull/{number}#issuecomment-{comment_id}",
        },
        repository={"full_name": FULL, "name": REPO},
    )


async def _link_pr_to_task(session, *, pr_number: int = 88) -> None:
    session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=pr_number,
            github_issue_number=94,
            plaky_task_id=TASK,
            link_source="issue_keyword",
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_a_created_comment_is_mirrored_once(db_session, posted) -> None:
    await _link_pr_to_task(db_session)

    res = await rh.handle_issue_comment_on_pr(
        _comment_event("created", "Please add a retry here"), db_session
    )

    assert res.get("ok") is True
    assert len(posted) == 1
    assert "Please add a retry here" in posted[0][1]
    assert "edited" not in posted[0][1]


@pytest.mark.asyncio
async def test_an_edited_comment_reaches_the_board(db_session, posted) -> None:
    """The bug: this used to return "ignored non-created comment" and post nothing."""
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "Please add a retry"), db_session)

    res = await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "Please add a retry AND a timeout"), db_session
    )

    assert res.get("ok") is True
    assert len(posted) == 2, "the correction is on the board"
    assert "Please add a retry AND a timeout" in posted[1][1]
    assert "GitHub PR comment edited" in posted[1][1], "labelled, so it reads as a correction"


@pytest.mark.asyncio
async def test_the_same_edit_delivered_twice_posts_once(db_session, posted) -> None:
    """Webhook delivery is at-least-once. An edit must never duplicate on redelivery."""
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "original"), db_session)
    edited = _comment_event("edited", "corrected")

    await rh.handle_issue_comment_on_pr(edited, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)

    assert len(posted) == 2, "one create + one edit, however many deliveries arrive"


@pytest.mark.asyncio
async def test_editing_the_same_comment_twice_posts_each_wording_once(db_session, posted) -> None:
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "v1"), db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("edited", "v2"), db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("edited", "v3"), db_session)

    assert len(posted) == 3
    assert "v3" in posted[2][1]


@pytest.mark.asyncio
async def test_an_edit_that_changed_nothing_posts_nothing(db_session, posted) -> None:
    """GitHub sends `edited` for metadata changes too; unchanged text is not news."""
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "unchanged text"), db_session)

    res = await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "unchanged text"), db_session
    )

    assert len(posted) == 1
    assert res.get("skipped") is True or all(m.get("skipped") for m in res.get("mirrored", []))


@pytest.mark.asyncio
async def test_a_duplicate_create_delivery_posts_once(db_session, posted) -> None:
    await _link_pr_to_task(db_session)
    event = _comment_event("created", "only once please")

    await rh.handle_issue_comment_on_pr(event, db_session)
    await rh.handle_issue_comment_on_pr(event, db_session)

    assert len(posted) == 1


@pytest.mark.asyncio
async def test_two_different_comments_are_both_mirrored(db_session, posted) -> None:
    await _link_pr_to_task(db_session)

    await rh.handle_issue_comment_on_pr(
        _comment_event("created", "first", comment_id=111), db_session
    )
    await rh.handle_issue_comment_on_pr(
        _comment_event("created", "second", comment_id=222), db_session
    )

    assert len(posted) == 2


@pytest.mark.asyncio
async def test_the_github_url_and_commenter_survive_the_edit(db_session, posted) -> None:
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "v1"), db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("edited", "v2"), db_session)

    body = posted[1][1]
    assert "ali-ferris" in body
    assert f"#issuecomment-{COMMENT_ID}" in body, "the comment id stays traceable"


@pytest.mark.asyncio
async def test_the_comment_identity_is_recorded_for_dedupe(db_session, posted) -> None:
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "v1"), db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("edited", "v2"), db_session)

    rows = [
        row
        for row in (await db_session.execute(select(SyncLog))).scalars()
        if row.action == "pr_comment_synced"
    ]
    assert len(rows) == 2
    assert all(str(COMMENT_ID) in (row.detail or "") for row in rows)
    assert any('"revision": true' in (row.detail or "").lower() for row in rows)


@pytest.mark.asyncio
async def test_boardmans_own_comment_is_still_ignored_on_edit(db_session, posted) -> None:
    """Unchanged: Boardman posts as a support-team member and must not drive the board."""
    from boardman.github.pr_actions import with_marker

    await _link_pr_to_task(db_session)

    res = await rh.handle_issue_comment_on_pr(
        _comment_event("edited", with_marker("QA assigned: someone")), db_session
    )

    assert res.get("skipped") is True
    assert posted == []


@pytest.mark.asyncio
async def test_an_unhandled_action_is_still_ignored(db_session, posted) -> None:
    await _link_pr_to_task(db_session)

    res = await rh.handle_issue_comment_on_pr(_comment_event("deleted", "gone"), db_session)

    assert posted == []
    assert "deleted" in res.get("message", "")


# --- plain issue comments (not on a PR) ------------------------------------------------


async def _map_issue_to_task(session, *, issue_number: int = 94) -> None:
    session.add(
        IssueTaskMap(
            github_repo=REPO,
            github_issue_number=issue_number,
            plaky_task_id=TASK,
            plaky_task_url=f"https://app.plaky.com/i/{TASK}",
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_a_plain_issue_comment_edit_reaches_the_board_once(db_session, posted) -> None:
    await _map_issue_to_task(db_session)
    created = _comment_event("created", "issue note v1", is_pr=False, number=94)
    edited = _comment_event("edited", "issue note v2", is_pr=False, number=94)

    await rh.handle_issue_comment_on_pr(created, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)

    assert len(posted) == 2
    assert "GitHub comment edited" in posted[1][1]


@pytest.mark.asyncio
async def test_a_pause_request_added_by_an_edit_still_pauses(
    db_session, posted, monkeypatch
) -> None:
    """An edit is a real comment: the workflow it triggers must run, exactly once."""
    await _link_pr_to_task(db_session)
    paused: list[str] = []

    async def fake_resolve(bid, configured, intent):
        return ("status_key", "paused-id")

    async def fake_status(task_id, value, board_id, *, status_field_key=None):
        paused.append(task_id)
        return {"ok": True}

    monkeypatch.setattr(rh, "_resolve_status", fake_resolve)
    monkeypatch.setattr(rh, "_update_plaky_task_status", fake_status)

    await rh.handle_issue_comment_on_pr(_comment_event("created", "looks good"), db_session)
    assert paused == []

    await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "actually, pause this for now"), db_session
    )
    assert paused == [TASK]
