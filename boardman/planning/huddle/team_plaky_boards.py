from __future__ import annotations

import logging
from pathlib import Path

from boardman.planning.team_config import resolve_planning_mappings
from boardman.planning.team_models import (
    PlakyBoardRef,
    first_board,
    parse_board_ref,
    unique_boards,
    with_derived_board_teams,
)
from boardman.settings import settings

log = logging.getLogger(__name__)

__all__ = [
    "PlakyBoardRef",
    "load_team_plaky_boards",
    "board_for_team",
    "boards_for_team",
    "parse_board_ref",
    "with_derived_board_teams",
]


def load_team_plaky_boards(path: str | Path | None = None) -> dict[str, PlakyBoardRef]:
    report = resolve_planning_mappings(
        team_repos_file=settings.planning_team_repos_file,
        team_boards_file=path or settings.planning_team_plaky_boards_file,
    )
    if path is None:
        log.debug("team_plaky_boards_source=%s", report.team_boards_source)
    return report.team_boards


def board_for_team(mapping: dict[str, PlakyBoardRef], team_focus: str) -> PlakyBoardRef | None:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return mapping.get("all-teams") or first_board(mapping)
    if normalized == "it" and "it" not in mapping:
        return mapping.get("all-teams") or first_board(mapping)
    return mapping.get(normalized)


def boards_for_team(
    mapping: dict[str, PlakyBoardRef],
    team_focus: str,
) -> list[PlakyBoardRef]:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return unique_boards(mapping.values())
    if normalized == "it" and "it" not in mapping:
        return unique_boards(mapping.values())
    ref = mapping.get(normalized)
    return [ref] if ref else []
