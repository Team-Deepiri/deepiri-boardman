"""What the roster says when it cannot show every repo.

The roster's whole job is to stop Boardman claiming a real repo does not exist. A silent
truncation turns that guarantee into a lie for every name past the cap, so a truncated
roster has to say so -- to the model and, through it, to the user (Sorge review, PR #88).
"""

from __future__ import annotations

import pytest

from boardman.agent import org_roster
from boardman.settings import settings


@pytest.fixture()
def _wired(monkeypatch):
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    monkeypatch.setattr(settings, "github_pat", "t")
    monkeypatch.setattr("boardman.github.http.github_http_client", lambda: object())
    yield


def _with_repos(monkeypatch, count: int) -> None:
    names = [f"deepiri-org/repo{i:03d}" for i in range(count)]

    async def fake(*_a, **_k):
        return names

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", fake)


@pytest.mark.asyncio
async def test_a_complete_roster_claims_completeness(monkeypatch, _wired) -> None:
    _with_repos(monkeypatch, 3)
    out = await org_roster.org_repo_roster_markdown()
    assert "This is the full list." in out
    assert "not shown here" not in out
    assert "genuinely absent from this list" in out


@pytest.mark.asyncio
async def test_a_truncated_roster_says_how_many_are_missing(monkeypatch, _wired) -> None:
    monkeypatch.setattr(settings, "agent_org_roster_max_names", 5)
    _with_repos(monkeypatch, 12)
    out = await org_roster.org_repo_roster_markdown()

    assert "5 of the 12 repositories" in out
    assert "7 more are not shown here" in out
    assert "…and 7 more repositories not listed here" in out
    assert out.count("`repo") == 5


@pytest.mark.asyncio
async def test_a_truncated_roster_never_licenses_a_does_not_exist_answer(
    monkeypatch, _wired
) -> None:
    """Absence from a partial list is not evidence of absence -- the exact bug this fixes."""
    monkeypatch.setattr(settings, "agent_org_roster_max_names", 2)
    _with_repos(monkeypatch, 9)
    out = await org_roster.org_repo_roster_markdown()

    assert "the list above is truncated" in out
    assert "rather than claiming the repository does not exist" in out
    assert "genuinely absent from this list may you say you cannot find it" not in out


@pytest.mark.asyncio
async def test_an_unexpected_failure_still_degrades_to_no_roster(monkeypatch, _wired) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("this is a bug, not a blip")

    monkeypatch.setattr("boardman.github.org_repos.fetch_org_repository_full_names", boom)
    assert await org_roster.org_repo_roster_markdown() == ""
