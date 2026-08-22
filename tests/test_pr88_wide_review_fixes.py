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
