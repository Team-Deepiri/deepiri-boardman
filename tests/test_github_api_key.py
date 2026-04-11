"""Test GitHub API Key validation."""

from __future__ import annotations

import pytest

from boardman.github import team_roster


class TestGitHubApiKeyValidation:
    def test_github_pat_valid(self, monkeypatch):
        import os
        from boardman import settings as bs

        pat = os.environ.get("GITHUB_PAT")
        if not pat:
            pytest.skip("GITHUB_PAT not set in environment")

        monkeypatch.setattr(bs.settings, "github_pat", pat)
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is True, f"Expected ok=True, got: {result}"
        assert isinstance(result["members"], list)

    def test_github_pat_invalid(self, monkeypatch):
        from boardman import settings as bs

        monkeypatch.setattr(bs.settings, "github_pat", "invalid-token-12345")
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is False
        assert "error" in result["message"].lower() or "403" in result["message"] or "401" in result["message"]

    def test_github_pat_empty(self, monkeypatch):
        from boardman import settings as bs

        monkeypatch.setattr(bs.settings, "github_pat", "")
        team_roster.clear_support_team_cache()

        result = team_roster.fetch_support_team_members_sync(enrich_names=False)

        assert result["ok"] is False
        assert "not set" in result["message"].lower()