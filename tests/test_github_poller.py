"""TESTING_LIVE_PLAKY GitHub event poller: event mapping, baseline, push comments."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from boardman.services import github_poller as gp
from boardman.settings import settings


def test_poller_repos_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky_repos", "a/b, c/d\n a/b, not-a-repo")
    assert gp.poller_repos() == ["a/b", "c/d"]


def test_start_disabled_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "testing_live_plaky", False)
    assert gp.start_github_poller_if_enabled() is None


@pytest.mark.asyncio
async def test_issue_opened_event_routes_to_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def fake_handle(payload, session):
        calls.append(payload)
        return {"ok": True, "message": "created"}

    monkeypatch.setattr(gp, "handle_issue_opened", fake_handle)
    poller = gp.GitHubEventPoller()
    event = {
        "id": "100",
        "type": "IssuesEvent",
        "payload": {
            "action": "opened",
            "issue": {"number": 7, "title": "Bug", "html_url": "https://github.com/o/r/issues/7"},
        },
    }
    await poller._dispatch_event("Team-Deepiri/deepiri-boardman", event)
    assert len(calls) == 1
    payload = calls[0]
    assert payload.repository.full_name == "Team-Deepiri/deepiri-boardman"
    assert payload.repository.name == "deepiri-boardman"
    assert payload.issue.number == 7


@pytest.mark.asyncio
async def test_review_created_action_maps_to_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def fake_review(payload, session):
        calls.append(payload)
        return {"ok": True}

    monkeypatch.setattr(gp, "handle_pull_request_review", fake_review)
    poller = gp.GitHubEventPoller()
    event = {
        "id": "101",
        "type": "PullRequestReviewEvent",
        "payload": {
            "action": "created",
            "review": {"state": "approved", "user": {"login": "qa-person"}},
            "pull_request": {
                "number": 3,
                "title": "Fix",
                "html_url": "https://github.com/o/r/pull/3",
                "state": "open",
            },
        },
    }
    await poller._dispatch_event("o/r", event)
    assert len(calls) == 1
    assert calls[0].action == "submitted"


@pytest.mark.asyncio
async def test_events_feed_slim_review_pr_object_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Events-feed review events embed a slim pull_request (no title/html_url/state).

    Regression: the payload model must still parse and route (review handlers need the PR
    number + review state, not those fields)."""
    calls: list = []

    async def fake_review(payload, session):
        calls.append(payload)
        return {"ok": True}

    monkeypatch.setattr(gp, "handle_pull_request_review", fake_review)
    poller = gp.GitHubEventPoller()
    event = {
        "id": "11469027649",
        "type": "PullRequestReviewEvent",
        "payload": {
            "action": "created",
            "review": {"state": "approved", "user": {"login": "Blasted-ctrl"}},
            # Slim PR object exactly as the events feed sends it — no title/html_url/state.
            "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/49", "number": 49},
        },
    }
    await poller._dispatch_event("Team-Deepiri/deepiri-boardman", event)
    assert len(calls) == 1
    assert calls[0].pull_request.number == 49
    assert calls[0].review.state == "approved"


@pytest.mark.asyncio
async def test_pr_closed_merged_routes_to_merged_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    merged_calls: list[Any] = []
    closed_calls: list[Any] = []

    async def fake_merged(payload, session):
        merged_calls.append(payload)
        return {"ok": True}

    async def fake_closed(payload, session):
        closed_calls.append(payload)
        return {"ok": True}

    monkeypatch.setattr(gp, "handle_pr_merged", fake_merged)
    monkeypatch.setattr(gp, "handle_pr_closed_without_merge", fake_closed)
    poller = gp.GitHubEventPoller()

    def pr_event(merged: bool) -> dict:
        return {
            "id": "102",
            "type": "PullRequestEvent",
            "payload": {
                "action": "closed",
                "pull_request": {
                    "number": 4,
                    "title": "x",
                    "html_url": "https://github.com/o/r/pull/4",
                    "state": "closed",
                    "merged": merged,
                },
            },
        }

    await poller._dispatch_event("o/r", pr_event(True))
    await poller._dispatch_event("o/r", pr_event(False))
    assert len(merged_calls) == 1
    assert len(closed_calls) == 1


class _FakeResponse:
    def __init__(self, status_code: int, body: Any, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body


class _FakeClient:
    """Stands in for httpx.AsyncClient inside _poll_repo."""

    responses: list[_FakeResponse] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeClient.responses.pop(0)


@pytest.mark.asyncio
async def test_first_poll_sets_baseline_and_replays_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[dict] = []

    async def fake_dispatch(full_name, event):
        dispatched.append(event)

    monkeypatch.setattr(settings, "github_pat", "x" * 10)
    monkeypatch.setattr(settings, "testing_live_plaky_catchup_minutes", 0.0)
    monkeypatch.setattr(gp, "httpx", type("M", (), {"AsyncClient": _FakeClient}))
    poller = gp.GitHubEventPoller()
    monkeypatch.setattr(poller, "_dispatch_event", fake_dispatch)

    # Events feed only handles review/comment types now (issues/PRs come from the direct poll).
    events_page = [
        {"id": "205", "type": "IssueCommentEvent", "payload": {}, "created_at": "2026-07-07T02:00:00Z"},
        {"id": "204", "type": "IssueCommentEvent", "payload": {}, "created_at": "2026-07-07T01:00:00Z"},
    ]
    # First poll: baseline only, nothing dispatched.
    _FakeClient.responses = [_FakeResponse(200, events_page)]
    await poller._poll_repo("o/r")
    assert dispatched == []
    assert poller._seen_ids["o/r"] == {"205", "204"}

    # Second poll: one newer event -> dispatched exactly once.
    newer = [{"id": "207", "type": "IssueCommentEvent", "payload": {}, "created_at": "2026-07-07T03:00:00Z"}] + events_page
    _FakeClient.responses = [_FakeResponse(200, newer)]
    await poller._poll_repo("o/r")
    assert [e["id"] for e in dispatched] == ["207"]
    assert "207" in poller._seen_ids["o/r"]


@pytest.mark.asyncio
async def test_novelty_is_by_set_not_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: event ids are not comparable across types. A newer event with a LOWER
    numeric id than a baselined one must still be detected as fresh (set membership, not >)."""
    dispatched: list[dict] = []

    async def fake_dispatch(full_name, event):
        dispatched.append(event)

    monkeypatch.setattr(settings, "github_pat", "x" * 10)
    monkeypatch.setattr(settings, "testing_live_plaky_catchup_minutes", 0.0)
    monkeypatch.setattr(gp, "httpx", type("M", (), {"AsyncClient": _FakeClient}))
    poller = gp.GitHubEventPoller()
    monkeypatch.setattr(poller, "_dispatch_event", fake_dispatch)

    baseline = [{"id": "14458784314", "type": "IssueCommentEvent", "payload": {}, "created_at": "2026-07-07T02:39:10Z"}]
    _FakeClient.responses = [_FakeResponse(200, baseline)]
    await poller._poll_repo("o/r")
    assert dispatched == []

    # New comment: LOWER numeric id, LATER timestamp — still fresh.
    newer = [
        {"id": "11439882581", "type": "IssueCommentEvent", "payload": {"action": "created"}, "created_at": "2026-07-07T04:10:21Z"},
    ] + baseline
    _FakeClient.responses = [_FakeResponse(200, newer)]
    await poller._poll_repo("o/r")
    assert [e["id"] for e in dispatched] == ["11439882581"]


@pytest.mark.asyncio
async def test_catchup_window_processes_recent_events_on_first_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[dict] = []

    async def fake_dispatch(full_name, event):
        dispatched.append(event)

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

    monkeypatch.setattr(settings, "github_pat", "x" * 10)
    monkeypatch.setattr(settings, "testing_live_plaky_catchup_minutes", 45.0)
    monkeypatch.setattr(gp, "httpx", type("M", (), {"AsyncClient": _FakeClient}))
    poller = gp.GitHubEventPoller()
    monkeypatch.setattr(poller, "_dispatch_event", fake_dispatch)

    events = [
        {"id": "900", "type": "IssueCommentEvent", "payload": {"action": "created"}, "created_at": recent},
        {"id": "800", "type": "IssueCommentEvent", "payload": {"action": "created"}, "created_at": old},
    ]
    _FakeClient.responses = [_FakeResponse(200, events)]
    await poller._poll_repo("o/r")
    # Only the recent event is caught up; the 6h-old one stays baselined.
    assert [e["id"] for e in dispatched] == ["900"]


class _SimpleClient:
    """Fake httpx client for the direct-poll unit tests (returns one canned response)."""

    def __init__(self, resp: "_FakeResponse"):
        self._resp = resp

    async def get(self, url, headers=None):
        return self._resp


def _fresh_proc() -> dict:
    return {"issues_opened": set(), "prs_opened": set(), "prs_closed": set(), "commits": set()}


@pytest.mark.asyncio
async def test_direct_poll_issue_opened_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    handled: list = []

    async def fake_run(parsed):
        handled.append(parsed)
        return {"ok": True, "message": "created"}

    monkeypatch.setattr(settings, "github_pat", "x" * 10)
    poller = gp.GitHubEventPoller()
    monkeypatch.setattr(poller, "_run_handler", fake_run)

    baseline = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
    proc = _fresh_proc()
    issues = [
        {"number": 61, "title": "Test: Polling", "body": "b", "html_url": "u", "state": "open",
         "user": {"login": "Blasted-ctrl"}, "created_at": "2026-07-07T04:10:20Z"},
        # A PR shows up in /issues but must be skipped here (handled by _poll_pulls):
        {"number": 60, "title": "a PR", "pull_request": {"url": "x"}, "created_at": "2026-07-07T05:00:00Z"},
        # Created before baseline -> ignored:
        {"number": 10, "title": "old", "created_at": "2026-07-06T00:00:00Z"},
    ]
    client = _SimpleClient(_FakeResponse(200, issues))
    await poller._poll_issues(client, "Team-Deepiri/deepiri-boardman", baseline, "2026-07-07T00:00:00Z", proc)
    assert len(handled) == 1
    assert handled[0].issue.number == 61
    assert handled[0].action == "opened"
    # Second poll: same issue must not be reprocessed.
    await poller._poll_issues(client, "Team-Deepiri/deepiri-boardman", baseline, "2026-07-07T00:00:00Z", proc)
    assert len(handled) == 1


@pytest.mark.asyncio
async def test_direct_poll_pr_merged_and_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    handled: list = []

    async def fake_run(parsed):
        handled.append(parsed)
        return {"ok": True}

    monkeypatch.setattr(settings, "github_pat", "x" * 10)
    poller = gp.GitHubEventPoller()
    monkeypatch.setattr(poller, "_run_handler", fake_run)

    baseline = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
    proc = _fresh_proc()
    pulls = [
        # Opened after baseline AND still open:
        {"number": 70, "title": "new pr", "body": "Fixes #61", "html_url": "u", "state": "open",
         "draft": False, "created_at": "2026-07-07T03:00:00Z", "updated_at": "2026-07-07T03:00:00Z"},
        # Created before baseline but merged after -> merged (not opened):
        {"number": 49, "title": "lints", "body": "", "html_url": "u", "state": "closed",
         "merged_at": "2026-07-07T06:00:00Z", "draft": False,
         "created_at": "2026-07-06T00:00:00Z", "updated_at": "2026-07-07T06:00:00Z"},
    ]
    client = _SimpleClient(_FakeResponse(200, pulls))
    await poller._poll_pulls(client, "Team-Deepiri/deepiri-boardman", baseline, proc)

    actions = {(p.pull_request.number, p.action, p.pull_request.merged) for p in handled}
    assert (70, "opened", False) in actions
    assert (49, "closed", True) in actions
    # Dedupe on a second poll.
    await poller._poll_pulls(client, "Team-Deepiri/deepiri-boardman", baseline, proc)
    assert len(handled) == 2


@pytest.mark.asyncio
async def test_push_event_comments_on_linked_task(monkeypatch: pytest.MonkeyPatch) -> None:
    comments: list[tuple[str, str]] = []

    class FakeMapping:
        plaky_task_id = "task-9"

    async def fake_find(repo_name, num, session):
        return FakeMapping() if num == 12 else None

    class FakePlaky:
        def __init__(self):
            pass

        async def add_comment(self, task_id, body, **kw):
            comments.append((task_id, body))
            return {"ok": True}

    monkeypatch.setattr(gp, "find_plaky_task_by_issue", fake_find)
    monkeypatch.setattr("boardman.plaky.client.PlakyClient", FakePlaky)

    poller = gp.GitHubEventPoller()
    event = {
        "id": "300",
        "type": "PushEvent",
        "actor": {"login": "Blasted-ctrl"},
        "payload": {
            "commits": [
                {"sha": "abc1234def", "message": "Fixes #12 correct the sync"},
                {"sha": "ffff", "message": "no issue reference"},
            ]
        },
    }
    await poller._handle_push("Team-Deepiri/deepiri-boardman", event, event["payload"])
    assert len(comments) == 1
    task_id, body = comments[0]
    assert task_id == "task-9"
    assert "Blasted-ctrl" in body and "abc1234" in body
