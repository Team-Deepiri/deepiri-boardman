"""GitHub PR reviews and PR comments against a fake in-memory ClickUp (TASK_PROVIDER=clickup)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from boardman.database.models import IssueTaskMap, PullRequestTaskLink
from boardman.github.pr_actions import with_marker
from boardman.github.webhooks import IssueCommentEventPayload, PullRequestReviewEventPayload
from boardman.services import clickup_review_sync as rsync
from boardman.services import pr_handler as ph
from boardman.services import pr_review_handler as prh
from boardman.services.pr_task_registry import distinct_task_ids_for_pr
from tests.clickup_world import (  # noqa: F401  (the fixtures are registered by name)
    DEV,
    FULL,
    QA,
    REPO,
    _db,
    _ids,
    _pr,
    _status,
    _task,
    _world,
)

OTHER = "77"
SUPPORT = {"support-sam"}


@pytest.fixture
def gh(world, monkeypatch):
    """GitHub-side stand-ins the review sync reads: checks, author, merged, commit count, roster."""
    state: dict[str, Any] = {
        "failing": [],
        "author": "dev-ann",
        "merged": False,
        "commits": 5,
        "support": set(SUPPORT),
    }

    async def failing(full, pr):
        return list(state["failing"])

    async def author(full, pr):
        return state["author"]

    async def merged(full, pr):
        return state["merged"]

    async def commits(full, pr):
        return state["commits"]

    monkeypatch.setattr(rsync, "ClickUpClient", type(world.client))
    monkeypatch.setattr(rsync, "failing_required_checks", failing)
    monkeypatch.setattr(rsync, "pr_author_login", author)
    monkeypatch.setattr(rsync, "pr_is_merged", merged)
    monkeypatch.setattr(rsync, "current_commit_count", commits)
    monkeypatch.setattr(rsync, "support_team_logins_casefold", lambda: set(state["support"]))
    members = [
        SimpleNamespace(id=DEV, github_login="dev-ann", display="Ann Dev", roles=["dev"]),
        SimpleNamespace(id=QA, github_login="qa-quinn", display="Quinn QA", roles=["qa"]),
        SimpleNamespace(id=OTHER, github_login="qa-other", display="Other QA", roles=["qa"]),
    ]
    monkeypatch.setattr(
        rsync,
        "load_team_assignments",
        lambda: SimpleNamespace(members=members, fallback_members=[]),
    )
    return state


async def _opened(world, db, **kw):
    """A task with an open PR linked: QA is Quinn, the developer is Ann."""
    task = await _task(world, db, **kw)
    await ph.handle_pr_opened(_pr(), db)
    return task


def _review(state: str, login: str, body: str = "", *, action: str = "submitted", rid: int = 1):
    return PullRequestReviewEventPayload(
        action=action,
        review={
            "id": rid,
            "user": {"login": login},
            "state": state,
            "body": body,
            "html_url": "https://github.com/org/repo/pull/9#pullrequestreview-1",
            "submitted_at": "2026-10-04T10:00:00Z",
        },
        pull_request={"number": 9, "title": "Fix the thing", "body": "Fixes #7"},
        repository={"full_name": FULL, "name": REPO},
    )


def _comment(
    login: str,
    body: str = "looks fine",
    *,
    action: str = "created",
    issue: int = 9,
    is_pr: bool = True,
    cid: int = 1,
    changed: bool = True,
):
    return IssueCommentEventPayload(
        action=action,
        issue={"number": issue, "pull_request": {"url": "x"} if is_pr else None},
        comment={
            "id": cid,
            "body": body,
            "user": {"login": login},
            "html_url": "https://github.com/org/repo/pull/9#issuecomment-1",
            "created_at": "2026-10-04T10:00:00Z",
            "updated_at": "2026-10-04T10:00:00Z",
        },
        repository={"full_name": FULL, "name": REPO},
        changes=({"body": {"from": "old"}} if changed else {"title": {"from": "t"}})
        if action == "edited"
        else None,
    )


# -- reviews ------------------------------------------------------------------------------------------


async def test_an_approval_marks_the_task_approved_and_records_the_commit_baseline(world, db, gh):
    task = await _opened(world, db)
    out = await prh.handle_pull_request_review(_review("approved", "someone-else"), db)
    assert out["ok"] and _status(task) == "qa verified"
    link = (await db.execute(select(PullRequestTaskLink))).scalar_one()
    assert link.commits_at_last_review == 5


async def test_failing_checks_hold_an_approval_back(world, db, gh):
    task = await _opened(world, db)
    gh["failing"] = ["unit-tests"]
    out = await prh.handle_pull_request_review(_review("approved", "qa-quinn"), db)
    assert out["skipped"] is True and "failing checks" in out["message"]
    assert _status(task) == "needs qa"


async def test_changes_requested_counts_only_from_the_assigned_qa(world, db, gh):
    task = await _opened(world, db)
    out = await prh.handle_pull_request_review(_review("changes_requested", "qa-other"), db)
    assert out["skipped"] is True and "not the assigned QA" in out["message"]
    assert _status(task) == "needs qa"
    out = await prh.handle_pull_request_review(_review("changes_requested", "qa-quinn", rid=2), db)
    assert out["ok"] and _status(task) == "qa rejected"


async def test_changes_requested_from_someone_unmappable_is_ignored(world, db, gh):
    task = await _opened(world, db)
    out = await prh.handle_pull_request_review(_review("changes_requested", "stranger"), db)
    assert out["skipped"] is True and "could not map reviewer" in out["message"]
    assert _status(task) == "needs qa"


async def test_a_comment_review_means_in_qa_only_for_support_or_the_assigned_qa(world, db, gh):
    task = await _opened(world, db)
    await prh.handle_pull_request_review(_review("commented", "drive-by", rid=2), db)
    assert _status(task) == "needs qa"
    await prh.handle_pull_request_review(_review("commented", "support-sam", rid=3), db)
    assert _status(task) == "in qa"
    task["status"] = {"status": "needs qa"}
    await prh.handle_pull_request_review(_review("commented", "qa-quinn", rid=4), db)  # assigned QA
    assert _status(task) == "in qa"


async def test_review_text_is_mirrored_once_and_bots_are_skipped(world, db, gh):
    task = await _opened(world, db)
    rv = _review("commented", "support-sam", "please add a test")
    await prh.handle_pull_request_review(rv, db)
    await prh.handle_pull_request_review(rv, db)  # redelivery
    mirrored = [c for c in task["comments"] if "GitHub PR review" in c]
    assert len(mirrored) == 1 and "please add a test" in mirrored[0]
    before = len(task["comments"])
    await prh.handle_pull_request_review(_review("commented", "lint[bot]", "noise", rid=9), db)
    assert len(task["comments"]) == before


async def test_a_dismissed_approval_goes_back_to_in_qa(world, db, gh):
    task = await _opened(world, db)
    await prh.handle_pull_request_review(_review("approved", "qa-quinn"), db)
    assert _status(task) == "qa verified"
    out = await prh.handle_pull_request_review(
        _review("approved", "qa-quinn", action="dismissed"), db
    )
    assert out["event"] == "review_dismissed" and _status(task) == "in qa"
    task["status"] = {"status": "qa rejected"}  # only an approval is walked back
    await prh.handle_pull_request_review(_review("approved", "qa-quinn", action="dismissed"), db)
    assert _status(task) == "qa rejected"


async def test_reviews_for_an_unlinked_pr_and_other_actions_are_skipped(world, db, gh):
    out = await prh.handle_pull_request_review(_review("approved", "qa-quinn"), db)
    assert out["skipped"] is True and "no ClickUp task" in out["message"]
    out = await prh.handle_pull_request_review(_review("approved", "qa-quinn", action="edited"), db)
    assert "ignored non-submitted" in out["message"]


async def test_an_unconfigured_verdict_status_is_reported_not_guessed(world, db, gh, monkeypatch):
    task = await _opened(world, db)
    monkeypatch.setattr(
        rsync.status_for_intent.__globals__["settings"], "clickup_status_approved", ""
    )
    out = await prh.handle_pull_request_review(_review("approved", "qa-quinn"), db)
    assert out["skipped"] is True and "no matching QA status" in out["message"]
    assert _status(task) == "needs qa"


# -- conversation comments on a PR ----------------------------------------------------------------------


async def test_the_assigned_qa_commenting_means_in_qa(world, db, gh):
    task = await _opened(world, db)
    out = await prh.handle_issue_comment_on_pr(_comment("qa-quinn"), db)
    assert out["ok"] and _status(task) == "in qa"


async def test_a_support_member_who_is_not_the_author_means_in_qa_but_the_author_never_does(
    world, db, gh
):
    task = await _opened(world, db)
    gh["support"] = {"support-sam", "dev-ann"}
    out = await prh.handle_issue_comment_on_pr(_comment("dev-ann", "pushed a fix", cid=2), db)
    assert out["skipped"] is True and out["pr_author_matches_commenter"] is True
    assert _status(task) == "needs qa"
    await prh.handle_issue_comment_on_pr(_comment("support-sam", cid=3), db)
    assert _status(task) == "in qa"


async def test_an_unreadable_pr_author_fails_closed(world, db, gh):
    task = await _opened(world, db)
    gh["author"] = ""
    out = await prh.handle_issue_comment_on_pr(_comment("support-sam"), db)
    assert out["skipped"] is True and _status(task) == "needs qa"


async def test_a_plain_dev_comment_changes_nothing_but_is_mirrored(world, db, gh):
    task = await _opened(world, db)
    out = await prh.handle_issue_comment_on_pr(_comment("random-dev", "thanks!"), db)
    assert out["skipped"] is True and _status(task) == "needs qa"
    assert any("GitHub PR comment" in c for c in task["comments"])


async def test_a_dev_commenting_after_a_verdict_means_revisions_in_progress(world, db, gh):
    task = await _opened(world, db)
    task["status"] = {"status": "qa rejected"}
    out = await prh.handle_issue_comment_on_pr(_comment("random-dev", "fixing now"), db)
    assert out["event"] == "revisions_in_progress" and _status(task) == "in progress"


async def test_that_resume_never_applies_to_a_merged_pr_or_a_finished_task(world, db, gh):
    task = await _opened(world, db)
    task["status"] = {"status": "qa verified"}
    gh["merged"] = True
    out = await prh.handle_issue_comment_on_pr(_comment("random-dev", "late note", cid=2), db)
    assert out["skipped"] is True and _status(task) == "qa verified"
    gh["merged"] = False
    task["status"] = {"status": "complete"}
    await prh.handle_issue_comment_on_pr(_comment("random-dev", "another", cid=3), db)
    assert _status(task) == "complete"


async def test_the_assigned_qa_commenting_after_a_verdict_is_in_qa_not_revisions(world, db, gh):
    task = await _opened(world, db)
    task["status"] = {"status": "qa verified"}
    await prh.handle_issue_comment_on_pr(_comment("qa-quinn", "reopening review"), db)
    assert _status(task) == "in qa"


async def test_anyone_saying_pause_pauses_and_is_skipped_without_a_paused_status(
    world, db, gh, monkeypatch
):
    task = await _opened(world, db)
    out = await prh.handle_issue_comment_on_pr(_comment("random-dev", "pausing this for now"), db)
    assert out["skipped"] is True and "no paused status" in out["message"]
    monkeypatch.setattr(
        rsync.status_for_intent.__globals__["settings"], "clickup_status_paused", "on hold"
    )
    out = await prh.handle_issue_comment_on_pr(
        _comment("random-dev", "pausing this for now", cid=2), db
    )
    assert out["event"] == "paused" and _status(task) == "on hold"


async def test_a_dev_pinging_qa_means_needs_qa_again_but_a_qa_pinging_does_not(world, db, gh):
    task = await _opened(world, db)
    task["status"] = {"status": "qa rejected"}
    out = await prh.handle_issue_comment_on_pr(_comment("random-dev", "@qa-quinn ready again"), db)
    assert out["event"] == "needs_qa_again" and _status(task) == "needs qa"
    task["status"] = {"status": "in progress"}
    out = await prh.handle_issue_comment_on_pr(_comment("qa-other", "@qa-quinn FYI", cid=2), db)
    assert out.get("event") != "needs_qa_again"


async def test_an_edited_comment_updates_the_record_never_the_state(world, db, gh):
    task = await _opened(world, db)
    await prh.handle_issue_comment_on_pr(_comment("qa-quinn"), db)
    task["status"] = {"status": "qa verified"}
    out = await prh.handle_issue_comment_on_pr(
        _comment("qa-quinn", "reworded", action="edited"), db
    )
    assert out["event"] == "pr_comment_edit_mirrored" and _status(task) == "qa verified"
    out = await prh.handle_issue_comment_on_pr(
        _comment("qa-quinn", "same", action="edited", changed=False), db
    )
    assert out["skipped"] is True


async def test_bots_and_boardman_comments_are_ignored(world, db, gh):
    task = await _opened(world, db)
    before = len(task["comments"])
    assert (await prh.handle_issue_comment_on_pr(_comment("ci[bot]"), db))["skipped"] is True
    assert len(task["comments"]) == before
    mine = _comment("qa-quinn", with_marker("you've been assigned as **QA reviewer**"), cid=2)
    out = await prh.handle_issue_comment_on_pr(mine, db)
    assert out["skipped"] is True and _status(task) == "needs qa"


async def test_a_comment_on_an_unlinked_pr_is_skipped(world, db, gh):
    out = await prh.handle_issue_comment_on_pr(_comment("qa-quinn"), db)
    assert out["skipped"] is True and "no ClickUp task" in out["message"]


async def test_a_comment_on_a_plain_issue_lands_on_its_task(world, db, gh):
    task = await _task(world, db)
    out = await prh.handle_issue_comment_on_pr(
        _comment("someone", "any update?", issue=7, is_pr=False), db
    )
    assert out["event"] == "issue_comment_synced"
    assert any("GitHub comment" in c and "any update?" in c for c in task["comments"])
    again = await prh.handle_issue_comment_on_pr(
        _comment("someone", "any update?", issue=7, is_pr=False), db
    )
    assert again["skipped"] is True
    assert len([c for c in task["comments"] if "any update?" in c]) == 1
    gone = await prh.handle_issue_comment_on_pr(_comment("x", "hi", issue=404, is_pr=False), db)
    assert gone["skipped"] is True


async def test_a_comment_with_the_users_field_configured_reads_the_assigned_qa_from_it(
    world, db, gh, monkeypatch
):
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_qa_field_id", "FLD")
    monkeypatch.setattr("boardman.services.clickup_task_ops.settings.clickup_qa_field_id", "FLD")
    task = await _opened(world, db)
    assert task["custom_fields"] == [{"id": "FLD", "value": [{"id": QA}]}]
    await prh.handle_issue_comment_on_pr(_comment("qa-quinn"), db)
    assert _status(task) == "in qa"


async def test_both_review_entry_points_dispatch_to_clickup(world, db, gh, monkeypatch):
    called: list[str] = []

    def fake(name):
        async def inner(*a, **kw):
            called.append(name)
            return {"ok": True}

        return inner

    for name in (
        "handle_pull_request_review",
        "handle_issue_comment_on_pr",
        "sync_plain_issue_comment",
    ):
        monkeypatch.setattr(rsync, name, fake(name))
    await prh.handle_pull_request_review(_review("approved", "x"), db)
    await prh.handle_issue_comment_on_pr(_comment("x"), db)
    await prh._sync_plain_issue_comment(_comment("x", is_pr=False), db)
    assert called == [
        "handle_pull_request_review",
        "handle_issue_comment_on_pr",
        "sync_plain_issue_comment",
    ]


async def test_helpers_used_by_the_fixtures_exist(world, db, gh):
    task = await _opened(world, db)
    assert _ids(task) and (await db.execute(select(IssueTaskMap))).scalar_one()
    assert await distinct_task_ids_for_pr(db, github_repo=REPO, github_pr_number=9) == [task["id"]]
