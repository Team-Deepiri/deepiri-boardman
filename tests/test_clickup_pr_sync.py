"""GitHub pull-request webhooks against a fake in-memory ClickUp (TASK_PROVIDER=clickup)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman import task_provider
from boardman.database.models import Base, IssueTaskMap, PullRequestTaskLink, SyncLog
from boardman.github.webhooks import (
    DeploymentStatusEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
)
from boardman.services import clickup_pr_sync as sync
from boardman.services import pr_handler as ph
from boardman.services.pr_task_registry import stamp_commits_at_last_review
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


@pytest.fixture
def world(monkeypatch):
    fake = FakeClickUp()
    gh: dict[str, list] = {"comments": [], "reviewers": []}
    picks: list[dict[str, Any]] = []

    for name, value in STATUSES.items():
        monkeypatch.setattr(sync.settings, f"clickup_status_{name}", value)
    monkeypatch.setattr(sync.settings, "clickup_qa_field_id", "")
    monkeypatch.setattr(task_provider.settings, "task_provider", "clickup")
    monkeypatch.setattr(sync.settings, "plaky_skip_needs_qa_for_draft", True)
    monkeypatch.setattr(sync.settings, "plaky_complete_when_all_prs_merged", True)

    members = [
        SimpleNamespace(id=DEV, github_login="dev-ann", display="Ann Dev", roles=["dev"]),
        SimpleNamespace(id=QA, github_login="qa-quinn", display="Quinn QA", roles=["qa"]),
    ]
    monkeypatch.setattr(
        sync,
        "load_team_assignments",
        lambda: SimpleNamespace(members=members, qa_bug_specialist=""),
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
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_github_user_to_user_id", resolve)
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: (pid, ""),
    )
    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", reviewers)
    monkeypatch.setattr("boardman.github.pr_actions.has_qa_assignment_comment", has_comment)
    monkeypatch.setattr(ph.settings, "pr_task_sync_skip_bot_authors", False, raising=False)
    # The sync functions build their own client; route it to the fake.
    monkeypatch.setattr(sync, "ClickUpClient", lambda: fake.client())
    monkeypatch.setattr(
        "boardman.clickup.client.ClickUpClient.__init__", _init_for(fake), raising=True
    )
    return SimpleNamespace(fake=fake, gh=gh, picks=picks, client=fake.client())


def _init_for(fake):
    original = __import__(
        "boardman.clickup.client", fromlist=["ClickUpClient"]
    ).ClickUpClient.__init__

    def init(self, *a, **kw):
        original(
            self,
            "tok",
            "https://cu.test/api/v2",
            transport=__import__("httpx").MockTransport(fake.handler),
        )

    return init


@pytest_asyncio.fixture()
async def db():
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


# -- opened -----------------------------------------------------------------------------------------


async def test_opened_links_the_pr_and_runs_the_whole_workflow(world, db):
    task = await _task(world, db)
    r = await ph.handle_pr_opened(_pr(labels=["bug"]), db)
    assert r["ok"] and r["linked"] == [{"issue": 7, "task_id": task["id"]}]
    assert any("PR Opened" in c for c in task.get("comments", []))
    assert "type:bug" in [t["name"] for t in task["tags"]]
    assert _ids(task) == sorted([DEV, QA])  # the developer is filled and QA is added
    assert world.picks == [{"repo": FULL, "exclude": "dev-ann"}]  # never the PR's own author
    assert _status(task) == "needs qa"  # a new PR asks for review, last
    assert world.gh["reviewers"] == [(9, ["qa-quinn"])]
    assert "@qa-quinn" in world.gh["comments"][0][1]
    link = (await db.execute(select(PullRequestTaskLink))).scalar_one()
    assert (link.plaky_task_id, link.qa_plaky_id) == (task["id"], QA)


async def test_a_draft_pr_does_not_ask_for_qa_yet(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(draft=True), db)
    assert _status(task) == "assigned"  # owner filled, but not in the QA queue


async def test_a_replayed_open_never_moves_work_backwards_or_re_picks_qa(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["status"] = {"status": "in qa"}
    await ph.handle_pr_opened(_pr(), db, is_replay=True)
    assert _status(task) == "in qa"
    assert len(world.picks) == 1 and len(world.gh["comments"]) == 1  # QA is never overwritten


async def test_a_reopened_pr_says_so_and_stays_put(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["status"] = {"status": "qa verified"}
    await ph.handle_pr_opened(_pr("reopened"), db)
    assert _status(task) == "qa verified"
    assert any("PR Reopened" in c for c in task["comments"])


async def test_an_existing_owner_is_never_replaced(world, db):
    task = await _task(world, db, assignees=[99])
    await ph.handle_pr_opened(_pr(), db)
    assert "99" in _ids(task) and DEV not in _ids(task)


async def test_an_author_who_is_not_an_eligible_developer_is_not_assigned(world, db, monkeypatch):
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: ("", "QA-only account"),
    )
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    assert DEV not in _ids(task) and _status(task) == "needs qa"


async def test_a_pr_naming_no_issue_with_a_task_is_skipped_and_says_why(world, db):
    r = await ph.handle_pr_opened(_pr(body="no keyword here", head_ref="feat/x"), db)
    assert r["skipped"] is True and "No linked issues" in r["message"]
    r = await ph.handle_pr_opened(_pr(body="Fixes #404", number=10), db)
    assert r["skipped"] is True and "none has a ClickUp task" in r["message"]


async def test_the_qa_users_field_is_used_when_configured(world, db, monkeypatch):
    monkeypatch.setattr(sync.settings, "clickup_qa_field_id", "FLD")
    monkeypatch.setattr("boardman.clickup.client.settings.clickup_qa_field_id", "FLD")
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    assert task["custom_fields"] == [{"id": "FLD", "value": [{"id": QA}]}]
    assert QA not in _ids(task)  # QA is in the field, not a second assignee
    await ph.handle_pr_opened(_pr(), db, is_replay=True)
    assert len(world.picks) == 1


async def test_a_late_link_to_a_finished_task_does_not_stage_review_work(world, db):
    task = await _task(world, db, status="complete")
    mapping = (await db.execute(select(IssueTaskMap))).scalar_one()
    ok = await sync.link_pr_to_issue_task(
        db,
        world.client,
        payload=_pr(),
        issue_number=7,
        mapping=mapping,
        is_draft=False,
        headline="**PR Opened:**",
        is_late_link=True,
        skip_qa_if_finished=True,
    )
    assert ok and not world.picks and _status(task) == "complete"


# -- edited / labels ------------------------------------------------------------------------------------


async def test_an_edit_resyncs_the_developer_but_not_a_manual_owner(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["assignees"] = [{"id": "99"}]  # a lead reassigned by hand
    r = await ph.handle_pr_edited(_pr("edited", title="Better title"), db)
    assert r["event"] == "pr_metadata_synced"
    assert _ids(task) == ["99"]
    assert task["name"] == "[repo] Slow"  # a card linked to an issue keeps its own title


async def test_an_edit_that_makes_it_a_draft_never_moves_started_work_back(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["status"] = {"status": "in qa"}
    await ph.handle_pr_edited(_pr("edited", draft=True), db)
    assert _status(task) == "in qa"


async def test_labels_keep_exactly_one_type_tag(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(labels=["bug"]), db)
    await ph.handle_pr_labels_changed(_pr("labeled", labels=["enhancement"]), db)
    types = [t["name"] for t in task["tags"] if t["name"].startswith("type:")]
    assert len(types) == 1 and types[0] != "type:bug"


async def test_removing_the_only_type_label_leaves_the_type_alone(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(labels=["bug"]), db)
    r = await ph.handle_pr_labels_changed(_pr("unlabeled", labels=[], label={"name": "bug"}), db)
    assert r["skipped"] is True and "type:bug" in [t["name"] for t in task["tags"]]


# -- draft / ready / review requests / pushes -------------------------------------------------------------


async def test_converted_to_draft_pulls_only_needs_qa_back_to_in_progress(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    assert _status(task) == "needs qa"
    await ph.handle_pr_converted_to_draft(_pr("converted_to_draft", draft=True), db)
    assert _status(task) == "in progress"
    task["status"] = {"status": "qa verified"}
    await ph.handle_pr_converted_to_draft(_pr("converted_to_draft", draft=True), db)
    assert _status(task) == "qa verified"


async def test_ready_for_review_asks_for_qa(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(draft=True), db)
    assert _status(task) == "assigned"
    r = await ph.handle_pr_ready_for_review(_pr("ready_for_review"), db)
    assert r["event"] == "ready_for_review" and _status(task) == "needs qa"


async def test_asking_for_a_review_is_not_qa_engaging(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["status"] = {"status": "assigned"}
    r = await ph.handle_pr_review_requested(_pr("review_requested"), db)
    assert r["skipped"] is True and _status(task) == "assigned"


async def test_a_withdrawn_review_request_requeues_but_never_over_a_verdict(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    task["status"] = {"status": "in qa"}
    await ph.handle_pr_review_requested(_pr("review_request_removed"), db)
    assert _status(task) == "needs qa"
    task["status"] = {"status": "qa verified"}
    await ph.handle_pr_review_requested(_pr("review_request_removed"), db)
    assert _status(task) == "qa verified"
    task["status"] = {"status": "complete"}
    await ph.handle_pr_review_requested(_pr("review_request_removed"), db)
    assert _status(task) == "complete"


async def test_pushes_before_any_verdict_do_nothing(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    r = await ph.handle_pr_synchronized(_pr("synchronize", commits=4), db)
    assert r["skipped"] is True and "no QA verdict" in r["message"]
    assert _status(task) == "needs qa"


async def test_a_few_commits_after_a_verdict_mean_revisions_in_progress(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(commits=2), db)
    await stamp_commits_at_last_review(db, github_repo=REPO, github_pr_number=9, commits=2)
    await db.commit()
    task["status"] = {"status": "qa rejected"}
    r = await ph.handle_pr_synchronized(_pr("synchronize", commits=4), db)
    assert r["event"] == "revisions_in_progress" and _status(task) == "in progress"


async def test_many_commits_after_a_verdict_mean_needs_qa_again(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(commits=2), db)
    await stamp_commits_at_last_review(db, github_repo=REPO, github_pr_number=9, commits=2)
    await db.commit()
    task["status"] = {"status": "qa verified"}
    r = await ph.handle_pr_synchronized(_pr("synchronize", commits=9), db)
    assert r["event"] == "resubmitted_needs_qa_again" and _status(task) == "needs qa"


async def test_a_push_never_drags_an_unrelated_status_anywhere(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(commits=2), db)
    await stamp_commits_at_last_review(db, github_repo=REPO, github_pr_number=9, commits=2)
    await db.commit()
    task["status"] = {"status": "to do"}  # not a reviewed or in-progress state
    await ph.handle_pr_synchronized(_pr("synchronize", commits=9), db)
    assert _status(task) == "to do"


# -- closed / merged / deployed -------------------------------------------------------------------------


async def test_closing_the_last_open_pr_reverts_the_review_queue(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    r = await ph.handle_pr_closed_without_merge(_pr("closed", state="closed"), db)
    assert r["withdrawn_links"] == 1 and _status(task) == "in progress"


async def test_closing_leaves_a_verdict_and_other_open_prs_alone(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    await ph.handle_pr_opened(_pr(number=10), db)
    await ph.handle_pr_closed_without_merge(_pr("closed", state="closed"), db)
    assert _status(task) == "needs qa"  # PR #10 is still open
    await ph.handle_pr_closed_without_merge(_pr("closed", number=10, state="closed"), db)
    assert _status(task) == "in progress"
    task["status"] = {"status": "qa verified"}
    await ph.handle_pr_opened(_pr(number=11), db)
    task["status"] = {"status": "qa verified"}
    await ph.handle_pr_closed_without_merge(_pr("closed", number=11, state="closed"), db)
    assert _status(task) == "qa verified"


async def test_merge_completes_the_task_the_description_closes_once(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    merged = _pr("closed", state="closed", merged=True)
    r = await ph.handle_pr_merged(merged, db)
    assert r["updated"][0]["ok"] and _status(task) == "complete"
    task["status"] = {"status": "in progress"}  # a person moves it back after the merge
    r = await ph.handle_pr_merged(merged, db)
    assert r["updated"][0].get("already_applied") and _status(task) == "in progress"


async def test_a_title_only_reference_does_not_complete_on_merge(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(body="", title="Fixes #7: slow"), db)
    r = await ph.handle_pr_merged(
        _pr("closed", body="", title="Fixes #7: slow", state="closed", merged=True), db
    )
    assert r["updated"][0]["reason"] == "weak_link" and _status(task) != "complete"


async def test_merge_waits_for_the_tasks_other_open_pr(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    await ph.handle_pr_opened(_pr(number=10), db)
    r = await ph.handle_pr_merged(_pr("closed", state="closed", merged=True), db)
    assert r["updated"][0]["deferred"] is True and _status(task) != "complete"
    r = await ph.handle_pr_merged(_pr("closed", number=10, state="closed", merged=True), db)
    assert _status(task) == "complete"


async def test_a_failed_completion_is_retried_not_remembered_as_done(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    real = world.fake.handler
    world.fake.handler = lambda req: (
        __import__("httpx").Response(500, text="down") if req.method == "PUT" else real(req)
    )
    merged = _pr("closed", state="closed", merged=True)
    r = await ph.handle_pr_merged(merged, db)
    assert r["updated"][0]["ok"] is False and _status(task) != "complete"
    world.fake.handler = real
    r = await ph.handle_pr_merged(merged, db)
    assert r["updated"][0]["ok"] is True and _status(task) == "complete"


async def test_a_merge_with_no_status_configured_reports_instead_of_guessing(
    world, db, monkeypatch
):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    monkeypatch.setattr(sync.settings, "clickup_status_completed", "")
    r = await ph.handle_pr_merged(_pr("closed", state="closed", merged=True), db)
    assert "no completed status" in r["updated"][0]["skipped"] and _status(task) != "complete"


async def test_a_successful_deployment_moves_the_merged_task_to_deployed(world, db, monkeypatch):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)

    async def shas(full, sha):
        return [9]

    monkeypatch.setattr("boardman.services.clickup_pr_sync.prs_for_commit_sha", shas)
    ok = DeploymentStatusEventPayload(
        deployment={"sha": "abc", "environment": "prod"},
        deployment_status={"state": "success"},
        repository={"full_name": FULL, "name": REPO},
    )
    r = await ph.handle_deployment_status(ok, db)
    assert r["event"] == "deployed" and _status(task) == "deployed"
    task["status"] = {"status": "complete"}
    failed = ok.model_copy(update={"deployment_status": {"state": "failure"}})
    await ph.handle_deployment_status(
        DeploymentStatusEventPayload(
            deployment={"sha": "abc"},
            deployment_status={"state": "failure"},
            repository={"full_name": FULL, "name": REPO},
        ),
        db,
    )
    assert _status(task) == "complete" and failed is not None


# -- review comments ------------------------------------------------------------------------------------


def _inline(login: str, body: str = "please rename this", *, action: str = "created", cid: int = 1):
    return PullRequestReviewCommentEventPayload(
        action=action,
        comment={
            "id": cid,
            "body": body,
            "user": {"login": login},
            "html_url": "https://github.com/org/repo/pull/9#r1",
            "updated_at": "t",
        },
        pull_request={
            "number": 9,
            "title": "Fix the thing",
            "body": "Fixes #7",
            "html_url": "https://github.com/org/repo/pull/9",
        },
        repository={"full_name": FULL, "name": REPO},
    )


async def test_the_assigned_qa_commenting_means_in_qa_and_the_comment_is_mirrored_once(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    r = await ph.handle_pr_review_comment(_inline("qa-quinn"), db)
    assert r["updated"][0]["action"] == "in_qa_comment" and _status(task) == "in qa"
    await ph.handle_pr_review_comment(_inline("qa-quinn"), db)  # redelivery
    mirrored = [c for c in task["comments"] if "inline review comment" in c]
    assert len(mirrored) == 1 and "please rename this" in mirrored[0]


async def test_someone_else_commenting_mirrors_but_does_not_move_the_task(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    await ph.handle_pr_review_comment(_inline("random-person"), db)
    assert _status(task) == "needs qa"
    assert any("inline review comment" in c for c in task["comments"])


async def test_bot_and_boardman_comments_are_ignored(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    before = len(task["comments"])
    assert (await ph.handle_pr_review_comment(_inline("review-bot[bot]"), db))["skipped"] is True
    assert len(task["comments"]) == before


async def test_editing_a_comment_updates_the_record_not_the_state(world, db):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    await ph.handle_pr_review_comment(_inline("qa-quinn"), db)
    task["status"] = {"status": "qa verified"}
    r = await ph.handle_pr_review_comment(_inline("qa-quinn", "reworded", action="edited"), db)
    assert r["event"] == "pr_review_comment_edit_mirrored" and _status(task) == "qa verified"


# -- dispatch -------------------------------------------------------------------------------------------


async def test_every_pr_handler_dispatches_to_clickup(world, db, monkeypatch):
    called: list[str] = []

    def fake(name):
        async def inner(*a, **kw):
            called.append(name)
            return {"ok": True}

        return inner

    for name in (
        "handle_pr_opened",
        "handle_pr_converted_to_draft",
        "handle_pr_ready_for_review",
        "handle_pr_review_requested",
        "handle_pr_synchronized",
        "handle_pr_closed_without_merge",
        "handle_deployment_status",
        "handle_pr_merged",
        "handle_pr_review_comment",
        "handle_pr_labels_changed",
    ):
        monkeypatch.setattr(sync, name, fake(name))
    pr, dep, inline = (
        _pr(),
        DeploymentStatusEventPayload(
            deployment={"sha": "a"},
            deployment_status={"state": "success"},
            repository={"full_name": FULL, "name": REPO},
        ),
        _inline("x"),
    )
    await ph.handle_pr_opened(pr, db)
    await ph.handle_pr_converted_to_draft(pr, db)
    await ph.handle_pr_ready_for_review(pr, db)
    await ph.handle_pr_review_requested(pr, db)
    await ph.handle_pr_synchronized(pr, db)
    await ph.handle_pr_closed_without_merge(pr, db)
    await ph.handle_deployment_status(dep, db)
    await ph.handle_pr_merged(pr, db)
    await ph.handle_pr_review_comment(inline, db)
    await ph.handle_pr_labels_changed(pr, db)
    assert len(called) == 10 and len(set(called)) == 10


async def test_plaky_stays_the_default(world, db, monkeypatch):
    monkeypatch.setattr(task_provider.settings, "task_provider", "plaky")
    assert task_provider.active_provider() == "plaky"
    assert (await db.execute(select(SyncLog))).scalars().all() == []
