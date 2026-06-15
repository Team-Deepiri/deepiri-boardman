from boardman.planning.team_repos import load_team_repos, repos_for_team


def test_repos_for_team_all_teams_dedupes() -> None:
    mapping = {
        "ai-ml": ["repo-a"],
        "qa": ["repo-b", "repo-a"],
    }
    repos = repos_for_team(mapping, "all-teams")
    assert repos == ["repo-a", "repo-b"]


def test_load_team_repos_uses_defaults_when_missing(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(
        "boardman.planning.team_repos.settings.planning_team_repos_file",
        str(missing),
    )
    mapping = load_team_repos()
    assert "ai-ml" in mapping
    assert mapping["ai-ml"]
