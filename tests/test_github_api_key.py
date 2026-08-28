"""Test GitHub API Key validation.

Two of these call GitHub for real -- one to prove the configured PAT works, one to prove a
rejected PAT is reported rather than swallowed. Both are opt-in behind BOARDMAN_GITHUB_LIVE
so an ordinary `pytest` run never depends on the network or on a credential being present.
Running the suite used to reach GitHub here on every invocation.
"""

from __future__ import annotations

import os

import pytest

from boardman.github import team_roster

_LIVE = os.environ.get("BOARDMAN_GITHUB_LIVE", "").strip() not in ("", "0", "false", "False")
requires_live_github = pytest.mark.skipif(
    not _LIVE, reason="Live GitHub check: set BOARDMAN_GITHUB_LIVE=1 to run"
)


class TestGitHubApiKeyValidation:
    @requires_live_github
    def test_github_pat_valid(self, monkeypatch):
        from boardman import settings as bs

        pat = os.environ.get("GITHUB_PAT")
        if not pat:
            pytest.skip("GITHUB_PAT not set in environment")

        monkeypatch.setattr(bs.settings, "github_pat", pat)
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is True, f"Expected ok=True, got: {result}"
        assert isinstance(result["members"], list)

    @requires_live_github
    def test_github_pat_invalid(self, monkeypatch):
        from boardman import settings as bs

        monkeypatch.setattr(bs.settings, "github_pat", "invalid-token-12345")
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is False
        assert (
            "error" in result["message"].lower()
            or "403" in result["message"]
            or "401" in result["message"]
        )

    def test_github_pat_empty(self, monkeypatch):
        from boardman import settings as bs

        monkeypatch.setattr(bs.settings, "github_pat", "")
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is False
        assert "not set" in result["message"].lower()
