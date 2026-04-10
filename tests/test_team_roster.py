"""GitHub support-team spec parsing."""

import pytest

from boardman.github.team_roster import parse_github_team_spec


def test_parse_github_team_spec():
    assert parse_github_team_spec("Team-Deepiri/support-team") == ("Team-Deepiri", "support-team")
    assert parse_github_team_spec("  Org-Name  /  my-team  ") == ("Org-Name", "my-team")
    assert parse_github_team_spec("") is None
    assert parse_github_team_spec("nope") is None


@pytest.mark.asyncio
async def test_fetch_support_team_requires_pat(monkeypatch):
    from boardman.github import team_roster as tr
    from boardman.settings import settings

    monkeypatch.setattr(settings, "github_pat", "")
    monkeypatch.setattr(settings, "github_support_team", "Team-Deepiri/support-team")
    r = await tr.fetch_support_team_members()
    assert r["ok"] is False
    assert "GITHUB_PAT" in r["message"]
