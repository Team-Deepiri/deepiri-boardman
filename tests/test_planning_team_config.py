from __future__ import annotations

import yaml

from boardman.repos_config import (
    category_to_team_focus,
    derive_team_boards_from_repos_yml,
    derive_team_repos_from_repos_yml,
    reload_repos_config,
)


def test_category_to_team_focus_aliases() -> None:
    assert category_to_team_focus("ai") == "ai-ml"
    assert category_to_team_focus("infrastructure") == "frontend-backend-infra"
    assert category_to_team_focus("qa") == "qa"
    assert category_to_team_focus("unknown") is None


def test_derive_team_mappings_from_repos_yml(tmp_path, monkeypatch) -> None:
    repos_file = tmp_path / "repos.yml"
    repos_file.write_text(
        yaml.safe_dump(
            {
                "repos": {
                    "deepiri-modelkit": {
                        "category": "ai-ml",
                        "plaky_board_id": "board-ai",
                    },
                    "deepiri-platform": {
                        "category": "qa",
                        "plaky_board_id": "board-qa",
                    },
                    "deepiri-api-gateway": {
                        "category": "backend",
                        "plaky_board_id": "board-platform",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("boardman.settings.settings.repos_yml_path", str(repos_file))
    monkeypatch.setattr(
        "boardman.planning.team_config.settings.planning_team_repos_file",
        str(tmp_path / "missing-team-repos.json"),
    )
    monkeypatch.setattr(
        "boardman.planning.team_config.settings.planning_team_plaky_boards_file",
        str(tmp_path / "missing-team-boards.json"),
    )
    reload_repos_config()

    repos = derive_team_repos_from_repos_yml()
    boards = derive_team_boards_from_repos_yml()

    assert repos["ai-ml"] == ["deepiri-modelkit"]
    assert repos["qa"] == ["deepiri-platform"]
    assert "deepiri-api-gateway" in repos["frontend-backend-infra"]
    assert boards["ai-ml"].board_id == "board-ai"
    assert boards["qa"].board_id == "board-qa"


def test_resolve_planning_mappings_prefers_json_override(tmp_path, monkeypatch) -> None:
    repos_json = tmp_path / "team_repos.json"
    repos_json.write_text('{"qa": ["override-repo"]}\n', encoding="utf-8")
    boards_json = tmp_path / "team_plaky_boards.json"
    boards_json.write_text('{"qa": {"board_id": "override-board"}}\n', encoding="utf-8")
    monkeypatch.setattr(
        "boardman.planning.team_config.settings.planning_team_repos_file",
        str(repos_json),
    )
    monkeypatch.setattr(
        "boardman.planning.team_config.settings.planning_team_plaky_boards_file",
        str(boards_json),
    )

    from boardman.planning.team_config import resolve_planning_mappings

    report = resolve_planning_mappings()
    assert report.team_repos_source == "json:team_repos.json"
    assert report.team_boards_source == "json:team_plaky_boards.json"
    assert report.team_repos["qa"] == ["override-repo"]
    assert report.team_boards["qa"].board_id == "override-board"
