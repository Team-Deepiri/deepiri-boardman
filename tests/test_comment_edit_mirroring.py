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
    updated_at: str = "2026-08-21T10:00:00Z",
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
            "updated_at": updated_at,
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


@pytest.fixture()
def pause_calls(monkeypatch):
    calls: list[str] = []

    async def fake_resolve(bid, configured, intent):
        return ("status_key", "paused-id")

    async def fake_status(task_id, value, board_id, *, status_field_key=None):
        calls.append(task_id)
        return {"ok": True}

    monkeypatch.setattr(rh, "_resolve_status", fake_resolve)
    monkeypatch.setattr(rh, "_update_plaky_task_status", fake_status)
    return calls


@pytest.mark.asyncio
async def test_a_new_pause_comment_pauses(db_session, posted, pause_calls) -> None:
    await _link_pr_to_task(db_session)

    await rh.handle_issue_comment_on_pr(_comment_event("created", "looks good"), db_session)
    assert pause_calls == []

    await rh.handle_issue_comment_on_pr(
        _comment_event("created", "pause this for now", comment_id=999), db_session
    )
    assert pause_calls == [TASK]


@pytest.mark.asyncio
async def test_editing_an_old_comment_does_not_re_drive_the_workflow(
    db_session, posted, pause_calls
) -> None:
    """A typo fix on a week-old "pausing this" must not drag a finished task back."""
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(
        _comment_event("created", "pausing this while we discuss"), db_session
    )
    assert pause_calls == [TASK]
    pause_calls.clear()

    res = await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "pausing this while we discuss it"), db_session
    )

    assert pause_calls == [], "the state machine does not re-run on an edit"
    assert res["event"] == "pr_comment_edit_mirrored"
    assert len(posted) == 2, "but the correction IS on the board"


@pytest.mark.asyncio
async def test_an_edited_inline_review_comment_does_not_re_drive_the_workflow(
    db_session, monkeypatch
) -> None:
    """The inline-review path accepts edits too, and had the same hole: fixing a typo in
    an old review comment dragged a QA-Verified task back to In QA."""
    from boardman.github.webhooks import PullRequestReviewCommentEventPayload
    from boardman.services import pr_handler as ph

    await _link_pr_to_task(db_session)
    posted_bodies: list[str] = []
    status_writes: list[str] = []

    class FakePlaky:
        async def add_comment(self, task_id, body, *, board_id=None):
            posted_bodies.append(body)
            return {"ok": True}

        async def get_board_item_public(self, *_a, **_k):
            return {"ok": False, "item": None}

    class Routing:
        plaky_board_id = "269031"
        plaky_group_id = "g1"

    async def fake_routing(*_a, **_k):
        return Routing()

    async def fake_status(task_id, *_a, **_k):
        status_writes.append(task_id)
        return {"ok": True}

    monkeypatch.setattr(ph, "PlakyClient", lambda *a, **k: FakePlaky())
    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr(ph, "_update_plaky_task_status", fake_status)

    def _event(action: str, body: str):
        return PullRequestReviewCommentEventPayload(
            action=action,
            comment={
                "id": 4242,
                "body": body,
                "user": {"login": "qa-person"},
                "html_url": f"https://github.com/{FULL}/pull/88#discussion_r4242",
            },
            pull_request={
                "number": 88,
                "title": "T",
                "body": "Fixes #94",
                "html_url": f"https://github.com/{FULL}/pull/88",
                "state": "open",
                "merged": False,
                "user": {"login": "ali-ferris"},
            },
            repository={"full_name": FULL, "name": REPO},
        )

    await ph.handle_pr_review_comment(_event("created", "please add a retry"), db_session)
    status_writes.clear()

    res = await ph.handle_pr_review_comment(
        _event("edited", "please add a retry and a timeout"), db_session
    )

    assert res["event"] == "pr_review_comment_edit_mirrored"
    assert status_writes == [], "an edit updates the record, not the state"
    assert any("edited" in b for b in posted_bodies), "the correction is still on the board"


@pytest.mark.asyncio
async def test_reverting_a_comment_reaches_the_board(db_session, posted) -> None:
    """Editing back to an earlier wording is news: the board's newest entry would
    otherwise keep showing text the author had taken back."""
    await _link_pr_to_task(db_session)

    await rh.handle_issue_comment_on_pr(
        _comment_event("created", "unblocked, ship it", updated_at="2026-08-21T10:00:00Z"),
        db_session,
    )
    await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "actually hold on", updated_at="2026-08-21T11:00:00Z"),
        db_session,
    )
    await rh.handle_issue_comment_on_pr(
        _comment_event("edited", "unblocked, ship it", updated_at="2026-08-21T12:00:00Z"),
        db_session,
    )

    assert len(posted) == 3
    assert "unblocked, ship it" in posted[2][1], "the revert is the newest thing on the board"


@pytest.mark.asyncio
async def test_a_redelivered_edit_still_posts_once(db_session, posted) -> None:
    """The same edit carries the same updated_at however many times GitHub sends it."""
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "v1"), db_session)
    edited = _comment_event("edited", "v2", updated_at="2026-08-21T11:00:00Z")

    await rh.handle_issue_comment_on_pr(edited, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)
    await rh.handle_issue_comment_on_pr(edited, db_session)

    assert len(posted) == 2


@pytest.mark.asyncio
async def test_a_comment_plaky_refused_is_retried_not_written_off(db_session) -> None:
    """A dedupe row is a record that the board HAS this comment. Writing one for a post
    Plaky refused makes the failure permanent: nothing is on the card, and every
    redelivery -- GitHub's retries, a poller catch-up, a reconciliation sweep -- matches
    the row and skips. The "PR Opened" notice went through here, so a single transient
    Plaky error silenced the notice for that PR for good.
    """
    from boardman.services.comment_dedupe import mirror_github_activity

    attempts: list[str] = []

    class FlakyPlaky:
        def __init__(self) -> None:
            self.fail = True

        async def add_comment(self, _tid, body, **_kw):
            attempts.append(body)
            if self.fail:
                return {"ok": False, "error": "502 from Plaky"}
            return {"ok": True}

    plaky = FlakyPlaky()
    marker = f"github:pr-link-notice:{REPO}:88:94"

    first = await mirror_github_activity(
        db_session,
        plaky,
        task_id=TASK,
        action="pr_link_notice",
        marker=marker,
        body="**PR Opened** #88",
        github_repo=REPO,
        github_ref="88",
    )
    assert first["ok"] is False and first["mirrored"] is False

    # The attempt is still on the record, under an action nothing dedupes against.
    rows = (await db_session.execute(select(SyncLog))).scalars().all()
    assert [r.action for r in rows] == ["pr_link_notice_failed"]

    plaky.fail = False
    second = await mirror_github_activity(
        db_session,
        plaky,
        task_id=TASK,
        action="pr_link_notice",
        marker=marker,
        body="**PR Opened** #88",
        github_repo=REPO,
        github_ref="88",
    )
    assert second["mirrored"] is True, "the redelivery was refused by the failed attempt"
    assert len(attempts) == 2

    # And now that it IS on the card, a third delivery is recognised and skipped.
    third = await mirror_github_activity(
        db_session,
        plaky,
        task_id=TASK,
        action="pr_link_notice",
        marker=marker,
        body="**PR Opened** #88",
        github_repo=REPO,
        github_ref="88",
    )
    assert third.get("skipped") is True
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_an_edit_with_a_new_timestamp_but_the_same_text_is_not_posted_again(
    db_session, posted
) -> None:
    """The revision key includes `updated_at`, which is what makes a REVERT mirror -- and
    what makes an edit that changed nothing look like new wording.

    GitHub sends `changes.body.from` only when the TEXT changed, so an edit that touched
    something else arrives with `changes` present and no `body` in it, carrying a fresh
    `updated_at`. Without reading that, the same sentence was posted to the card twice,
    and Plaky cannot delete a comment.
    """
    await _link_pr_to_task(db_session)
    await rh.handle_issue_comment_on_pr(_comment_event("created", "unchanged text"), db_session)
    assert len(posted) == 1

    later = _comment_event("edited", "unchanged text", updated_at="2026-08-21T11:30:00Z")
    later.changes = {"title": {"from": "something that is not the body"}}
    res = await rh.handle_issue_comment_on_pr(later, db_session)

    assert res.get("skipped") is True
    assert len(posted) == 1, "an edit that changed no text posted a second copy"

    # A real correction still lands.
    corrected = _comment_event("edited", "corrected text", updated_at="2026-08-21T11:40:00Z")
    corrected.changes = {"body": {"from": "unchanged text"}}
    await rh.handle_issue_comment_on_pr(corrected, db_session)
    assert len(posted) == 2

    # And a payload carrying no `changes` at all -- the poller synthesises those -- is
    # taken at its word rather than dropped.
    from_poller = _comment_event("edited", "corrected again", updated_at="2026-08-21T11:50:00Z")
    await rh.handle_issue_comment_on_pr(from_poller, db_session)
    assert len(posted) == 3
