from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from boardman.planning.team_models import (
    DEFAULT_TEAM_REPOS,
    PlakyBoardRef,
    with_derived_board_teams,
    with_derived_repo_teams,
)
from boardman.repos_config import (
    derive_team_boards_from_repos_yml,
    derive_team_repos_from_repos_yml,
)
from boardman.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanningMappingReport:
    team_repos_source: str
    team_boards_source: str
    team_repos_file: str
    team_boards_file: str
    repos_yml_path: str
    team_repos: dict[str, list[str]]
    team_boards: dict[str, PlakyBoardRef]


def _parse_team_repos_json(file_path: Path) -> dict[str, list[str]] | None:
    if not file_path.is_file():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("team_repos_load_failed path=%s error=%s", file_path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("team_repos_invalid_root path=%s", file_path)
        return None
    mapping: dict[str, list[str]] = {}
    for team, repos in raw.items():
        if isinstance(repos, list):
            mapping[str(team)] = [str(r).strip() for r in repos if str(r).strip()]
    return mapping or None


def _parse_team_boards_json(file_path: Path) -> dict[str, PlakyBoardRef] | None:
    from boardman.planning.team_models import parse_board_ref

    if not file_path.is_file():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("team_plaky_boards_load_failed path=%s error=%s", file_path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("team_plaky_boards_invalid_root path=%s", file_path)
        return None
    mapping: dict[str, PlakyBoardRef] = {}
    for team, value in raw.items():
        ref = parse_board_ref(value)
        if ref is not None:
            mapping[str(team)] = ref
    return mapping or None


def resolve_planning_mappings(
    *,
    team_repos_file: str | Path | None = None,
    team_boards_file: str | Path | None = None,
) -> PlanningMappingReport:
    repos_path = Path(team_repos_file or settings.planning_team_repos_file)
    boards_path = Path(team_boards_file or settings.planning_team_plaky_boards_file)

    parsed_repos = _parse_team_repos_json(repos_path)
    if parsed_repos:
        team_repos = with_derived_repo_teams(parsed_repos)
        repos_source = f"json:{repos_path.name}"
    else:
        derived_repos = derive_team_repos_from_repos_yml()
        if derived_repos:
            team_repos = derived_repos
            repos_source = f"repos.yml:{settings.repos_yml_path}"
        else:
            team_repos = with_derived_repo_teams(DEFAULT_TEAM_REPOS.copy())
            repos_source = "defaults"

    parsed_boards = _parse_team_boards_json(boards_path)
    if parsed_boards:
        team_boards = with_derived_board_teams(parsed_boards)
        boards_source = f"json:{boards_path.name}"
    else:
        derived_boards = derive_team_boards_from_repos_yml()
        team_boards = derived_boards
        boards_source = f"repos.yml:{settings.repos_yml_path}" if derived_boards else "none"

    return PlanningMappingReport(
        team_repos_source=repos_source,
        team_boards_source=boards_source,
        team_repos_file=str(repos_path),
        team_boards_file=str(boards_path),
        repos_yml_path=settings.repos_yml_path,
        team_repos=team_repos,
        team_boards=team_boards,
    )
