"""Ranking the org by open work.

The number that must never be misreported: GitHub's ``open_issues_count`` counts pull
requests as issues. Presenting it as an issue count would make Boardman confidently wrong
about every repo in the org at once.
"""

from __future__ import annotations

import pytest

from boardman.github import org_activity
from boardman.github.org_activity import format_activity_markdown, org_activity_ranking
from boardman.settings import settings

ROWS = [
    {"full_name": "o/busy", "open_issues_and_prs": 30, "pushed_at": "2026-08-20T10:00:00Z"},
    {"full_name": "o/quiet", "open_issues_and_prs": 1, "pushed_at": "2026-08-01T10:00:00Z"},
    {"full_name": "o/mid", "open_issues_and_prs": 9, "pushed_at": "2026-08-19T10:00:00Z"},
]


@pytest.fixture()
def _wired(monkeypatch):
    # Set explicitly rather than inheriting the real one from .env: the ranking needs an
    # org and a token, and depending on the ambient credential made this a live call.
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    monkeypatch.setattr(settings, "github_pat", "test-token")
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 2)
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
    # Credentials present but the org lists nothing: the distinct failure this pins.
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    monkeypatch.setattr(settings, "github_pat", "test-token")
    monkeypatch.setattr("boardman.github.http.github_http_client", lambda: object())
    monkeypatch.setattr("boardman.github.org_repos.cached_org_repo_rows", lambda *_a, **_k: [])

    async def no_names(*_a, **_k):
        return []

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", no_names)
    out = await org_activity_ranking()
    assert out["ok"] is False
    assert "could not list" in out["message"]


def test_markdown_of_a_failure_says_so() -> None:
    assert "Could not rank" in format_activity_markdown({"ok": False, "message": "nope"})


@pytest.mark.asyncio
async def test_the_split_depth_comes_from_settings_when_not_passed(monkeypatch, _wired) -> None:
    """How far down the ranking pays the extra call per repo is a deployment trade-off,
    not a literal in the module (Sorge review, PR #88)."""
    seen: list[str] = []

    async def fake_prs(_client, full_name, _headers):
        seen.append(full_name)
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 1)
    await org_activity_ranking(limit=5)
    assert seen == ["o/busy"], "only the configured head should pay for the split"

    seen.clear()
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 3)
    await org_activity_ranking(limit=5)
    assert seen == ["o/busy", "o/mid", "o/quiet"]


@pytest.mark.asyncio
async def test_an_explicit_split_top_still_wins(monkeypatch, _wired) -> None:
    seen: list[str] = []

    async def fake_prs(_client, full_name, _headers):
        seen.append(full_name)
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 3)
    await org_activity_ranking(limit=5, split_top=1)
    assert seen == ["o/busy"]


@pytest.mark.asyncio
async def test_split_top_zero_still_means_split_nothing(monkeypatch, _wired) -> None:
    """0 asked for no extra calls before this setting existed; it must keep meaning that."""
    seen: list[str] = []

    async def fake_prs(_client, full_name, _headers):
        seen.append(full_name)
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 8)
    out = await org_activity_ranking(limit=5, split_top=0)

    assert seen == []
    assert all("open_prs" not in row for row in out["ranked"])


@pytest.mark.asyncio
async def test_no_split_is_paid_for_a_row_the_limit_discards(monkeypatch, _wired) -> None:
    """Each split is one rate-limited GitHub call; spending it on a discarded row is waste."""
    seen: list[str] = []

    async def fake_prs(_client, full_name, _headers):
        seen.append(full_name)
        return 1

    monkeypatch.setattr(org_activity, "_open_pr_count", fake_prs)
    monkeypatch.setattr(settings, "github_org_activity_split_top_n", 8)
    out = await org_activity_ranking(limit=1)

    assert seen == ["o/busy"], "8 configured, but only 1 row is returned"
    assert len(out["ranked"]) == 1


@pytest.mark.asyncio
async def test_missing_credentials_are_reported_distinctly(monkeypatch) -> None:
    """ "no token" and "the org listed nothing" are different problems."""
    monkeypatch.setattr(settings, "github_pat", "")

    out = await org_activity_ranking()

    assert out["ok"] is False
    assert "GITHUB_ORG and a GitHub credential" in out["message"]
