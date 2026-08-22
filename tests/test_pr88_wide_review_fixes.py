"""Defects found reviewing PR #88 against main rather than against its own upstream.

Each of these is a path that quietly wrote the wrong thing to a live board, and none of
them had a test. Grouped here because they were found together, not because they share a
mechanism.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_a_label_that_merely_contains_a_severity_word_is_not_a_priority() -> None:
    """`high-availability` is not High and `low-code` is not Low.

    Searching for the word anywhere read those as explicit priorities, and an explicit
    priority is licensed to overwrite one a lead set on the board by hand.
    """
    from boardman.services.priority_rules import priority_from_github_label

    assert priority_from_github_label("high-availability") == ""
    assert priority_from_github_label("low-code") == ""
    assert priority_from_github_label("needs-medium-review") == ""
    assert priority_from_github_label("blocked-low-risk") == ""

    # The real ones still resolve.
    assert priority_from_github_label("high") == "High"
    assert priority_from_github_label("priority: high") == "High"
    assert priority_from_github_label("prio urgent") == "Very Important"
    assert priority_from_github_label("P1") == "High"


def test_good_first_issue_is_a_hint_not_a_priority_statement() -> None:
    """It still nudges the inferred value, which nothing overwrites; it no longer claims
    the right to replace a hand-set board priority."""
    from boardman.services.priority_rules import (
        infer_priority_from_text,
        priority_from_github_label,
    )

    assert priority_from_github_label("good first issue") == ""
    assert infer_priority_from_text("t", None, ["good first issue"]) == "Low"


def test_a_file_path_in_a_message_is_not_a_repo() -> None:
    """The answer is persisted to the session, so reading `boardman/agent/service.py` as
    the repo `boardman/agent` grounded every later question in a repo that does not
    exist."""
    import re

    pattern = re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![\w/-])")

    def repos(message: str) -> list[str]:
        return [t for t in pattern.findall(message) if "." not in t.rsplit("/", 1)[-1]]

    assert repos("look at boardman/agent/service.py") == []
    assert repos("see src/main.py for the bug") == []
    assert repos("check Team-Deepiri/deepiri-boardman please") == ["Team-Deepiri/deepiri-boardman"]


def test_only_the_tasks_that_failed_are_reported_as_failed() -> None:
    """A job that created four of five tasks told the user all five had failed, because
    the payload branch was read first and short-circuited the result."""
    import json

    from boardman.agent.write_failures import _titles

    payload = json.dumps({"tasks": [{"title": f"task {i}"} for i in range(5)]})
    result = json.dumps(
        {
            "results": [
                {"ok": True, "title": "task 0"},
                {"ok": True, "title": "task 1"},
                {"ok": False, "title": "task 2"},
                {"ok": True, "title": "task 3"},
                {"ok": True, "title": "task 4"},
            ]
        }
    )
    assert _titles(payload, result) == ["task 2"]

    # A job that died before producing a result still names the whole batch, which is the
    # honest answer when nothing knows which one failed.
    assert len(_titles(payload, None)) == 5


def test_the_open_pr_count_survives_a_repo_with_more_than_a_page_of_them() -> None:
    """One unpaginated page reported exactly 100 for any busier repo, and `open_issues` is
    derived by subtracting it -- so the repo looked like it had no issues."""
    import re

    link = (
        '<https://api.github.com/repositories/1/pulls?state=open&per_page=1&page=2>; rel="next", '
        '<https://api.github.com/repositories/1/pulls?state=open&per_page=1&page=137>; rel="last"'
    )
    last = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
    assert last and int(last.group(1)) == 137


def test_a_receipt_does_not_name_a_person_it_did_not_write() -> None:
    """The filter matched on the member id while the note carries the display name, so a
    person whose column an explicit field_values entry overrode was still named as
    assigned."""
    notes = [
        "assignee -> Ali Ferris (exact, 1.00) [person-5]",
        "qa -> David Poindexter (exact, 1.00) [person-6]",
    ]
    key = "person-5"
    kept = [n for n in notes if not n.endswith(f"[{key}]")]
    assert kept == ["qa -> David Poindexter (exact, 1.00) [person-6]"]


@pytest.mark.asyncio
async def test_a_merged_pr_is_not_reconciled_as_closed_without_merge(monkeypatch) -> None:
    """GitHub's LIST pulls endpoint omits the `merged` boolean, so every merged PR was
    routed to the closed-without-merge handler: links withdrawn and QA'd tasks dragged
    back to In Progress, every fifteen minutes, for work that shipped."""
    from boardman.services import reconcile as rc

    routed: list[str] = []

    async def fake_merged(payload: Any, _session: Any) -> dict[str, Any]:
        routed.append("merged")
        return {"ok": True, "updated": [{"task_id": "t"}]}

    async def fake_closed(payload: Any, _session: Any) -> dict[str, Any]:
        routed.append("closed_without_merge")
        return {"ok": True, "withdrawn_links": 1}

    async def linked(_session: Any, **_k: Any) -> list[str]:
        return ["t"]

    class _FakeGitHub:
        def __init__(self, pulls: list[dict[str, Any]]) -> None:
            self._pulls = pulls

        async def get(self, url: str, headers: Any = None):
            class R:
                status_code = 200

                def __init__(self, body: Any) -> None:
                    self._body = body

                def json(self) -> Any:
                    return self._body

            return R(self._pulls if "/pulls" in url else [])

        async def aclose(self) -> None:
            return None

    # A merged PR as the LIST endpoint reports it: merged_at set, no `merged` key.
    pulls = [
        {
            "number": 88,
            "title": "Retry the flaky upload",
            "body": "",
            "state": "closed",
            "merged_at": "2026-08-20T12:00:00Z",
            "updated_at": "2026-08-20T12:00:00Z",
            "user": {"login": "ali-ferris"},
            "head": {"ref": "feat/x"},
            "html_url": "https://github.com/Team-Deepiri/deepiri-boardman/pull/88",
        }
    ]

    monkeypatch.setattr(rc, "github_http_client", lambda: _FakeGitHub(pulls))
    monkeypatch.setattr(rc, "distinct_task_ids_for_pr", linked)
    monkeypatch.setattr(rc, "handle_pr_merged", fake_merged)
    monkeypatch.setattr(rc, "handle_pr_closed_without_merge", fake_closed)
    monkeypatch.setattr(rc.settings, "github_pat", "test-token")

    await rc.reconcile_repo("Team-Deepiri/deepiri-boardman", None)

    assert routed == ["merged"], routed


def test_a_directory_path_does_not_become_the_session_repo() -> None:
    """The resolved repo is persisted to the session, so a wrong one grounds every later
    turn. Rejecting only filenames was not enough: `tests/test_smoke` and
    `.github/workflows` both read as repos, and the assistant then presented them as
    real."""
    from boardman.agent.repo_resolution import resolve_repo

    def repo_of(message: str) -> str | None:
        return resolve_repo(message=message, explicit_repo=None, session_repo=None).repo

    assert repo_of("the flaky test is in tests/test_smoke, file a task") != "tests/test_smoke"
    assert repo_of("the workflow in .github/workflows is broken") != ".github/workflows"

    # A repo in the watched org is still taken from the message.
    named = resolve_repo(
        message="check Team-Deepiri/deepiri-boardman please", explicit_repo=None, session_repo=None
    )
    assert named.repo == "Team-Deepiri/deepiri-boardman" and named.source == "message"


def test_a_bare_repo_name_in_a_cache_key_is_still_purged() -> None:
    """A bare name is a supported tool argument and the key is built from whatever the
    caller passed, so `defects:boardman` survived every purge -- stale for the whole TTL
    beside owner/repo keys that had just been refreshed."""
    from boardman.github import read_cache

    assert read_cache._key_repo("defects:boardman") == "team-deepiri/boardman"
    assert read_cache._key_repo("planning:boardman:20") == "team-deepiri/boardman"
    assert read_cache._key_repo("structure:Team-Deepiri/X") == "team-deepiri/x"


def test_a_push_invalidates_what_is_cached_about_the_repo() -> None:
    """A push is the one event that changes the code, and it had no branch in the
    dispatcher: it fell through to the payload-model gate, which has no model for it, so
    the file trees and scans cached for that repo went on serving pre-push content."""
    import asyncio

    from boardman.routes.github_events import dispatch_github_event

    payload = {"repository": {"full_name": "Team-Deepiri/deepiri-boardman", "name": "x"}}
    result = asyncio.get_event_loop().run_until_complete(
        dispatch_github_event("push", payload, None)
    )
    assert result["ok"] is True
    assert result["repo"] == "Team-Deepiri/deepiri-boardman"


def test_a_bot_comment_does_not_reach_the_card() -> None:
    """A repo running CodeRabbit or Dependabot posts one comment per finding or per bump,
    and every one was landing on the card the QA reviewer reads."""
    import inspect

    from boardman.services import pr_review_handler as rh

    source = inspect.getsource(rh.handle_issue_comment_on_pr)
    assert 'commenter.endswith("[bot]")' in source, "the conversation path has no bot filter"

    review_source = inspect.getsource(rh.handle_pull_request_review)
    assert 'reviewer_login.endswith("[bot]")' in review_source


def test_a_failed_deferred_job_reports_the_same_status_from_either_runner() -> None:
    """The two runners race for the same job, so disagreeing about what a failed handler
    is made the reported status depend on which one claimed it -- and a client polling for
    "complete" read one of them as success."""
    import inspect

    from boardman.jobs import deferred

    source = inspect.getsource(deferred)
    assert 'status="complete" if succeeded else "incomplete"' in source


def test_a_plaky_limitation_is_not_reported_as_a_sync_failure() -> None:
    """Plaky has no verb for renaming an item, so a triage-created card's title can never
    be rewritten. Counting that refusal as a synchronization failure made every edit of
    such a PR return ok=False, which the webhook route answers with HTTP 500 -- and GitHub
    retries a 500, so one edit became a delivery loop.

    Exercised through the real shape `update_task_internal` returns: the refusal is
    reported per OPERATION, so a first version of this guard that read a top-level
    `error`/`message` never fired at all.
    """
    from boardman.services.pr_handler import _mutation_really_failed

    text_refused_only = {
        "ok": False,
        "operations": {
            "field_patch": {"ok": True},
            "item_text_fields": {
                "ok": False,
                "message": "Item title/description not set via item PATCH (unsupported)",
            },
        },
    }
    assert _mutation_really_failed(text_refused_only) is False

    # A field patch that genuinely failed is still a failure.
    real_failure = {
        "ok": False,
        "operations": {
            "field_patch": {"ok": False, "message": "500 from Plaky"},
            "item_text_fields": {"ok": False, "message": "unsupported"},
        },
    }
    assert _mutation_really_failed(real_failure) is True

    # And a refusal with nothing else attempted is not evidence of success.
    assert _mutation_really_failed({"ok": False, "operations": {}}) is True
    assert _mutation_really_failed({"ok": True, "operations": {}}) is False


def test_un_asking_for_a_review_moves_in_qa_back_to_needs_qa() -> None:
    """The event exists to take a card out of active review. A rank comparison rejected
    exactly that move (In QA outranks Needs QA) while allowing the write from Assigned,
    which pushes an unreviewed card INTO the queue -- the opposite of both intentions."""
    from boardman.services.pr_handler import _QA_VERDICT_INTENTS

    assert "workflow_in_qa" not in _QA_VERDICT_INTENTS, "In QA must still move to Needs QA"
    assert "workflow_assigned" not in _QA_VERDICT_INTENTS
    assert "github_pr_review_approved" in _QA_VERDICT_INTENTS, "a verdict is protected"
    assert "workflow_completed" in _QA_VERDICT_INTENTS


def test_the_project_context_snapshot_has_one_row_per_repo() -> None:
    """The lookups compared the raw value while the casefolded key was computed and thrown
    away, so "Team-Deepiri/X" and "team-deepiri/x" missed each other: two rows, two
    divergent snapshots, and whichever spelling the reader used won."""
    import inspect

    from boardman.agent import repo_context

    source = inspect.getsource(repo_context)
    assert "ProjectContext.repo == repo" not in source
    assert "ProjectContext.repo == key" in source
    assert "ProjectContext(repo=key)" in source


def test_the_fast_path_cannot_truncate_the_stream() -> None:
    """It reads Plaky live and the client re-raises transport errors. Raised from outside
    the block that emits the SSE error frame, an outage ended the stream with no error
    event at all."""
    import inspect

    from boardman.agent import service

    source = inspect.getsource(service)
    assert source.count('log_degraded(logger, "agent: fast path", exc)') == 2


def test_an_older_database_still_gets_the_issue_mapping_constraint() -> None:
    """`create_all` skips a table that exists, so the unique index that stops two
    concurrent deliveries filing two cards for one issue never reached a database made
    before it -- and the reservation guard depends on the IntegrityError it raises."""
    import inspect

    from boardman.database import session as db_session

    source = inspect.getsource(db_session)
    assert "uq_issue_task_map_repo_issue" in source
    assert "DELETE FROM issue_task_map WHERE id NOT IN" in source, "duplicates cleared first"


def test_a_diagnostic_read_failing_does_not_report_a_failed_write() -> None:
    """`field_diff` and `text_diff` record whether the PRE-write read succeeded. Folding
    them into the verdict made a successful write report failure, and the issue-reopen
    path reads that as "the restore did not take" and overwrites the just-restored status
    with the assignee ladder's Assigned -- the regression it exists to prevent."""
    import inspect

    from boardman.services import task_mutations

    source = inspect.getsource(task_mutations)
    assert '_DIAGNOSTIC_OPS = ("field_diff", "text_diff")' in source
    assert "k not in _DIAGNOSTIC_OPS" in source


def test_an_ordinary_follow_up_keeps_the_conversation_repo() -> None:
    """The unknown-slug guard's pattern matches ordinary English -- "what is in progress
    right now?" captures `progress` -- and it returned before the session fallback, so the
    turn ran with no repo context at all."""
    from boardman.agent.repo_resolution import resolve_repo

    session_repo = "Team-Deepiri/deepiri-boardman"
    for message in ("what is in progress right now?", "anything for QA?", "tasks from main"):
        out = resolve_repo(message=message, explicit_repo=None, session_repo=session_repo)
        assert out.repo == session_repo, f"{message!r} dropped the session repo"

    # With nothing to fall back on, refusing to guess is still right.
    out = resolve_repo(message="create a task for aarflingo", explicit_repo=None, session_repo=None)
    assert out.repo is None and out.source == "unknown-mentioned"


def test_a_named_but_unrouted_repo_does_not_borrow_another_board() -> None:
    """A repo the user named that has no repos.yml entry arrives as repo=None, which looks
    exactly like "no repo was mentioned" -- so the single-configured-board guess fired and
    filed that other repo's tasks into this one's group."""
    from boardman.agent.service import _resolve_placement

    board, group, note = _resolve_placement(None, None, None, repo_named_but_unresolved=True)
    assert board is None and group is None and note == ""


def test_a_write_post_does_not_follow_a_redirect() -> None:
    """The shared pool follows redirects and httpx turns a redirected POST into a bodyless
    GET, so after a repo rename the QA comment fetched the comment list, got 200, and
    reported a comment it never posted."""
    import inspect

    from boardman.github import pr_actions

    for fn in (pr_actions.comment_on_pr, pr_actions.request_reviewers):
        assert "follow_redirects=False" in inspect.getsource(fn), f"{fn.__name__} follows them"


def test_the_save_debounce_sentinel_is_not_a_boot_relative_zero() -> None:
    """`time.monotonic()` counts from boot, so 0.0 reads as "saved a moment ago" on a
    machine that has been up for less than the window -- and the first save, the one the
    sentinel exists to allow, was suppressed."""
    from boardman.github import qa_contribution_profile as qcp

    assert qcp._last_disk_save == float("-inf")
    qcp._last_disk_save = 1234.0
    qcp.clear_contribution_caches()
    assert qcp._last_disk_save == float("-inf")
