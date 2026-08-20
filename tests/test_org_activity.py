"""Ranking the org by open work.

The number that must never be misreported: GitHub's ``open_issues_count`` counts pull
requests as issues. Presenting it as an issue count would make Boardman confidently wrong
about every repo in the org at once.
"""

from __future__ import annotations

import pytest

from boardman.github import org_activity
from boardman.github.org_activity import format_activity_markdown, org_activity_ranking

ROWS = [
    {"full_name": "o/busy", "open_issues_and_prs": 30, "pushed_at": "2026-08-20T10:00:00Z"},
    {"full_name": "o/quiet", "open_issues_and_prs": 1, "pushed_at": "2026-08-01T10:00:00Z"},
    {"full_name": "o/mid", "open_issues_and_prs": 9, "pushed_at": "2026-08-19T10:00:00Z"},
]


@pytest.fixture()
def _wired(monkeypatch):
    monkeypatch.setattr(org_activity, "_SPLIT_TOP_N", 2)
    monkeypatch.setattr(
        "boardman.github.org_repos.cached_org_repo_rows", lambda *_a, **_k: list(ROWS)
    )
    monkeypatch.setattr("boardman.github.http.github_http_client", lambda: object())
    yield


@pytest.mark.asyncio
async def test_ranked_by_open_work_then_recency(monkeypatch, _wired) -> None:
    async def fake_prs(_client, full_name, _headers):
        return {"o/busy": 12, "o/mid": 4}.get(full_name)

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    out = await org_activity_ranking(limit=5, split_top=2)
    assert out["ok"] is True
    assert [r["full_name"] for r in out["ranked"]] == ["o/busy", "o/mid", "o/quiet"]


@pytest.mark.asyncio
async def test_issues_and_prs_are_split_for_the_head(monkeypatch, _wired) -> None:
    async def fake_prs(_client, full_name, _headers):
        return {"o/busy": 12, "o/mid": 4}.get(full_name)

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    out = await org_activity_ranking(limit=5, split_top=2)
    busy = out["ranked"][0]
    assert busy["open_prs"] == 12
    assert busy["open_issues"] == 18, "30 open items minus 12 PRs"


@pytest.mark.asyncio
async def test_an_unreadable_pr_count_is_null_not_zero(monkeypatch, _wired) -> None:
    """Zero pull requests and unknown are different facts."""

    async def fake_prs(_client, _full_name, _headers):
        return None

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    out = await org_activity_ranking(limit=3, split_top=1)
    head = out["ranked"][0]
    assert head["open_prs"] is None and head["open_issues"] is None
    assert "unavailable" in head["note"]


@pytest.mark.asyncio
async def test_the_tail_is_not_split_and_says_so(monkeypatch, _wired) -> None:
    async def fake_prs(_client, _full_name, _headers):
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    out = await org_activity_ranking(limit=5, split_top=1)
    tail = out["ranked"][1]
    assert "open_prs" not in tail, "the tail must not claim a split it never fetched"
    text = format_activity_markdown(out)
    assert "issues + PRs" in text


@pytest.mark.asyncio
async def test_the_counting_caveat_travels_with_the_data(monkeypatch, _wired) -> None:
    async def fake_prs(_client, _full_name, _headers):
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    out = await org_activity_ranking(limit=3)
    assert "INCLUDES pull requests" in out["counting_note"]


@pytest.mark.asyncio
async def test_no_repos_is_reported_not_faked(monkeypatch) -> None:
    monkeypatch.setattr("boardman.github.org_repos.cached_org_repo_rows", lambda *_a, **_k: [])

    async def no_names(*_a, **_k):
        return []

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", no_names)
    out = await org_activity_ranking()
    assert out["ok"] is False
    assert "could not list" in out["message"]


def test_markdown_of_a_failure_says_so() -> None:
    assert "Could not rank" in format_activity_markdown({"ok": False, "message": "nope"})
