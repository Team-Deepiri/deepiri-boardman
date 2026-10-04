"""PRs that name no issue: fuzzy matching against a ClickUp list, orphan triage, and the cleanup sweep."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from boardman.database.models import IssueTaskMap, PrTaskLifecycle, PullRequestTaskLink, SyncLog
from boardman.services import clickup_pr_sync as sync
from boardman.services import pr_handler as ph
from boardman.services import pr_task_lifecycle as lifecycle
from boardman.services import pr_task_linking as ptl
from boardman.services.pr_task_registry import mark_pr_merged
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

NO_ISSUE = "Adds retry logic to the uploader"


def _pipe(decision: str, task_id: str | None = None, score: float = 90.0):
    async def run(**kwargs):
        return ptl.PipelineResult(
            decision=decision,
            task_id=task_id,
            score=score,
            reason="test",
            top_scored=[
                ptl.ScoredCandidate(
                    task_id=task_id or "x",
                    title="Candidate",
                    description="",
                    score=score,
                    breakdown={},
                )
            ],
        )

    return run


async def _listed(world, db, **tasks):
    """Tasks already in the list (not mapped to any issue)."""
    out = {}
    for tid, (name, desc, status) in tasks.items():
        world.fake.tasks[tid] = {
            "id": tid,
            "name": name,
            "description": desc,
            "status": {"status": status},
            "assignees": [],
            "tags": [],
            "list": {"id": "L1"},
        }
        out[tid] = world.fake.tasks[tid]
    return out


# -- candidates and scoring -------------------------------------------------------------------------


async def test_candidates_come_from_the_issue_map_and_the_repos_own_list(world, db, monkeypatch):
    monkeypatch.setattr(ptl.settings, "pr_linking_fetch_board_items", True)
    mapped = await _task(world, db, issue=5)
    await _listed(world, db, tX=("Plain titled task", "no repo mention", "in progress"))
    cands = await ptl.gather_candidates_clickup(
        session=db,
        repo_name=REPO,
        repo_full=FULL,
        list_id="L1",
        client=world.client,
        repo_owns_list=True,
    )
    assert mapped["id"] in cands and "tX" in cands
    assert cands["tX"].status == "in progress"


async def test_a_shared_list_only_contributes_tasks_that_name_the_repo_or_an_issue(
    world, db, monkeypatch
):
    monkeypatch.setattr(ptl.settings, "pr_linking_fetch_board_items", True)
    await _listed(
        world,
        db,
        tA=("[repo] ours", "", "to do"),
        tB=("Someone else's work", "unrelated", "to do"),
        tC=("Fix", "see https://github.com/org/repo/issues/12", "to do"),
    )
    cands = await ptl.gather_candidates_clickup(
        session=db,
        repo_name=REPO,
        repo_full=FULL,
        list_id="L1",
        client=world.client,
        repo_owns_list=False,
    )
    assert set(cands) == {"tA", "tC"}


async def test_candidate_statuses_use_the_settings_spelling_so_the_scorer_can_match(
    world, db, monkeypatch
):
    monkeypatch.setattr(ptl.settings, "pr_linking_fetch_board_items", True)
    await _listed(world, db, tA=("[repo] a", "", "COMPLETE"))
    cands = await ptl.gather_candidates_clickup(
        session=db, repo_name=REPO, repo_full=FULL, list_id="L1", client=world.client
    )
    assert cands["tA"].status == "complete"


async def test_the_clickup_pipeline_links_a_clearly_matching_task_and_penalises_a_finished_one(
    world, db, monkeypatch
):
    monkeypatch.setattr(ptl.settings, "pr_linking_fetch_board_items", True)
    monkeypatch.setattr(ptl.settings, "pr_linking_pipeline_enabled", True)
    monkeypatch.setattr(ptl.settings, "pr_linking_llm_enabled", False)

    async def routing(*a, **k):
        from types import SimpleNamespace

        return SimpleNamespace(clickup_list_id="L1", category="")

    monkeypatch.setattr(ptl, "get_routing_async", routing)
    await _listed(
        world,
        db,
        live=(
            "[repo] Adds retry logic to the uploader",
            NO_ISSUE + " so uploads survive blips",
            "in progress",
        ),
        done=(
            "[repo] Adds retry logic to the uploader",
            NO_ISSUE + " so uploads survive blips",
            "complete",
        ),
    )
    result = await ptl.run_pr_task_pipeline_clickup(
        session=db,
        client=world.client,
        repo_full=FULL,
        repo_name=REPO,
        org="org",
        pr_number=9,
        pr_title=NO_ISSUE,
        pr_body=NO_ISSUE + " so uploads survive blips",
        head={"ref": "feat/retries"},
    )
    scores = {s.task_id: s.score for s in result.top_scored}
    assert scores["live"] > scores["done"]  # a finished task scores worse than the same live one
    assert result.top_scored[0].task_id == "live"


async def test_the_pipeline_is_off_when_disabled(world, db, monkeypatch):
    monkeypatch.setattr(ptl.settings, "pr_linking_pipeline_enabled", False)
    r = await ptl.run_pr_task_pipeline_clickup(
        session=db,
        client=world.client,
        repo_full=FULL,
        repo_name=REPO,
        org="o",
        pr_number=1,
        pr_title="t",
        pr_body="",
        head={"ref": "x"},
    )
    assert r.decision == "none" and r.reason == "pipeline_disabled"


# -- linking from the open handler --------------------------------------------------------------------


async def test_a_confident_fuzzy_match_links_the_pr_and_runs_the_workflow(world, db, monkeypatch):
    task = (await _listed(world, db, tM=("[repo] uploader retries", "", "to do")))["tM"]
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("auto_link", "tM"))
    r = await ph.handle_pr_opened(_pr(body="no keyword", title=NO_ISSUE), db)
    assert r["pipeline"] == "auto_link" and r["linked"] == [{"task_id": "tM", "via": "auto_link"}]
    assert any("automation link, auto_link" in c for c in task["comments"])
    assert DEV in _ids(task) and _status(task) == "needs qa"
    link = (await db.execute(select(PullRequestTaskLink))).scalar_one()
    assert (link.plaky_task_id, link.github_issue_number, link.link_source) == (
        "tM",
        0,
        "auto_link",
    )
    assert world.picks and world.gh["reviewers"]


async def test_a_replayed_fuzzy_link_never_moves_work_backwards(world, db, monkeypatch):
    task = (await _listed(world, db, tM=("[repo] uploader retries", "", "to do")))["tM"]
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("llm_link", "tM"))
    await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    task["status"] = {"status": "in qa"}
    await ph.handle_pr_edited(_pr("edited", body="still none", title=NO_ISSUE), db)
    assert _status(task) == "in qa"


async def test_a_replayed_open_that_fuzzy_matches_a_task_already_in_qa_leaves_it_there(
    world, db, monkeypatch
):
    task = (await _listed(world, db, tM=("[repo] uploader retries", "", "in qa")))["tM"]
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("auto_link", "tM"))
    await ph.handle_pr_opened(_pr(body="no keyword", title=NO_ISSUE), db, is_replay=True)
    assert _status(task) == "in qa"
    fresh = (await _listed(world, db, tN=("[repo] other", "", "in qa")))["tN"]
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("auto_link", "tN"))
    await ph.handle_pr_opened(_pr(body="no keyword", title=NO_ISSUE, number=10), db)
    assert _status(fresh) == "needs qa"  # a genuinely new PR does ask for review


async def test_a_triage_decision_without_ambiguous_pr_enabled_is_skipped(world, db, monkeypatch):
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    assert r["skipped"] is True and "no existing task matched" in r["message"]
    assert not world.fake.tasks


# -- orphan triage ------------------------------------------------------------------------------------


async def test_an_unmatched_pr_gets_a_real_task_linked_owned_and_in_the_qa_queue(
    world, db, monkeypatch
):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync.settings, "pr_task_cleanup_enabled", True)
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE, labels=["bug"]), db)
    assert r["created_from_pr"] and r["ambiguous_triage"]
    task = world.fake.tasks[r["plaky_task_id"]]
    assert task["name"] == NO_ISSUE and _status(task) == "needs qa"
    assert [t["name"] for t in task["tags"]] == ["repo", "type:bug"]
    assert DEV in _ids(task) and QA in _ids(task)
    assert "Closest existing candidates" in task["description"]
    assert any("created this task from the PR" in c for c in task["comments"])
    link = (await db.execute(select(PullRequestTaskLink))).scalar_one()
    assert link.link_source == "pr_task_created" and link.plaky_task_id == task["id"]
    assert (await db.execute(select(PrTaskLifecycle))).scalar_one().plaky_board_id == "LD"
    assert any(p == "/list/LD/task" for _, p, _ in world.fake.log)


async def test_triage_is_idempotent_per_pr(world, db, monkeypatch):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    await ph.handle_pr_edited(_pr("edited", body="none again", title=NO_ISSUE), db)
    await ph.handle_pr_opened(_pr("reopened", body="none", title=NO_ISSUE), db)
    assert len(world.fake.tasks) == 1


async def test_triage_does_not_recreate_a_task_the_cleanup_sweep_already_removed(
    world, db, monkeypatch
):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    first = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    # the cleanup sweep deleted the card and its link; a later replay must not conjure a new one
    world.fake.tasks.pop(first["plaky_task_id"])
    for row in (await db.execute(select(PullRequestTaskLink))).scalars():
        await db.delete(row)
    await db.commit()
    again = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    assert again["skipped"] is True and "already created" in again["message"]
    assert not world.fake.tasks


async def test_a_draft_orphan_pr_is_not_staged_for_qa(world, db, monkeypatch):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE, draft=True), db)
    assert _status(world.fake.tasks[r["plaky_task_id"]]) == "assigned"  # the author owns it


async def test_a_finished_pr_never_gets_a_manufactured_task(world, db, monkeypatch):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE, state="closed", merged=True), db)
    assert r["skipped"] is True and "already closed" in r["message"] and not world.fake.tasks


async def test_no_resolvable_list_skips_triage_with_a_reason(world, db, monkeypatch):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "")
    monkeypatch.setattr(sync.settings, "clickup_triage_list_id", "")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    assert r["skipped"] is True and "no ClickUp list resolvable" in r["message"]


async def test_the_triage_list_setting_wins_and_a_written_issue_is_claimed_for_the_new_card(
    world, db, monkeypatch
):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync.settings, "clickup_triage_list_id", "LT")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("none"))
    r = await ph.handle_pr_opened(_pr(body="Fixes #404", title=NO_ISSUE), db)
    assert any(p == "/list/LT/task" for _, p, _ in world.fake.log)
    claimed = (await db.execute(select(IssueTaskMap))).scalar_one()
    assert (claimed.github_issue_number, claimed.plaky_task_id) == (404, r["plaky_task_id"])
    # the issue's own events now update this card instead of opening a second one
    assert (
        await db.execute(select(SyncLog).where(SyncLog.action == "pr_ambiguous_triage"))
    ).scalar_one()


async def test_a_failed_create_releases_the_reservation_so_a_retry_can_run(world, db, monkeypatch):
    world.amb.enabled = True
    monkeypatch.setattr(sync.settings, "clickup_default_list_id", "LD")
    monkeypatch.setattr(sync, "run_pr_task_pipeline_clickup", _pipe("triage", None, 20.0))
    world.fake.fail_create = True
    r = await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db)
    assert r["ok"] is False
    await db.flush()
    assert (await db.execute(select(PullRequestTaskLink))).scalars().all() == []
    world.fake.fail_create = False
    assert (await ph.handle_pr_opened(_pr(body="none", title=NO_ISSUE), db))["created_from_pr"]


# -- the cleanup sweep ----------------------------------------------------------------------------------


async def test_cleanup_deletes_an_expired_orphan_task_in_clickup_and_keeps_the_audit_row(
    world, db, monkeypatch
):
    monkeypatch.setattr(lifecycle.settings, "pr_task_cleanup_enabled", True)
    await _listed(world, db, tO=("[repo] orphan", "", "needs qa"))
    db.add(
        PrTaskLifecycle(
            github_repo=REPO,
            github_pr_number=9,
            plaky_task_id="tO",
            plaky_board_id="LD",
            origin="created",
            cleanup_due_at=datetime.utcnow() - timedelta(days=1),
        )
    )
    await db.commit()
    out = await lifecycle.cleanup_orphaned_pr_tasks(db, world.client)
    assert out["deleted"] == 1 and "tO" in world.fake.deleted
    row = (await db.execute(select(PrTaskLifecycle))).scalar_one()
    assert row.deleted_at is not None


async def test_cleanup_leaves_unexpired_tasks_alone(world, db, monkeypatch):
    monkeypatch.setattr(lifecycle.settings, "pr_task_cleanup_enabled", True)
    await _listed(world, db, tO=("[repo] orphan", "", "needs qa"))
    db.add(
        PrTaskLifecycle(
            github_repo=REPO,
            github_pr_number=9,
            plaky_task_id="tO",
            plaky_board_id="LD",
            origin="created",
            cleanup_due_at=datetime.utcnow() + timedelta(days=3),
        )
    )
    await db.commit()
    assert (await lifecycle.cleanup_orphaned_pr_tasks(db, world.client))["deleted"] == 0
    assert not world.fake.deleted


async def test_archive_hides_only_completed_tasks_whose_pr_merged_and_is_opt_in(
    world, db, monkeypatch
):
    task = await _task(world, db)
    await ph.handle_pr_opened(_pr(), db)
    await mark_pr_merged(db, github_repo=REPO, github_pr_number=9)
    await db.commit()
    off = await lifecycle.archive_completed_matched_tasks(db, world.client)
    assert off["skipped"] is True
    monkeypatch.setattr(lifecycle.settings, "clickup_archive_completed_prs", True)
    none_yet = await lifecycle.archive_completed_matched_tasks(db, world.client)
    assert none_yet["archived"] == 0 and not task.get("archived")  # not completed yet
    task["status"] = {"status": "complete"}
    done = await lifecycle.archive_completed_matched_tasks(db, world.client)
    assert done["archived"] == 1 and task["archived"] is True
    again = await lifecycle.archive_completed_matched_tasks(db, world.client)
    assert again["archived"] == 0  # already archived


async def test_the_lifecycle_sweep_uses_clickup_when_it_is_the_provider(world, db, monkeypatch):
    monkeypatch.setattr(lifecycle.settings, "pr_task_cleanup_enabled", True)
    out = await lifecycle.cleanup_orphaned_pr_tasks(db)  # no client passed: built for ClickUp
    assert out == {"ok": True, "checked": 0, "deleted": 0, "failed": 0}
