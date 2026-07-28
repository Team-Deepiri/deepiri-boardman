from __future__ import annotations

import logging
from pathlib import Path

from boardman.planning.team_config import resolve_planning_mappings
from boardman.planning.team_models import (
    DEFAULT_TEAM_REPOS,
    TEAM_CHOICES,
    unique_repos,
    with_derived_repo_teams,
)
from boardman.settings import settings

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TEAM_REPOS",
    "TEAM_CHOICES",
    "load_team_repos",
    "repos_for_team",
    "with_derived_repo_teams",
]


def load_team_repos(path: str | Path | None = None) -> dict[str, list[str]]:
    """Load team → repo mapping from JSON override, repos.yml, or built-in defaults."""
    report = resolve_planning_mappings(
        team_repos_file=path or settings.planning_team_repos_file,
        team_boards_file=settings.planning_team_plaky_boards_file,
    )
    if path is None:
        log.debug("team_repos_source=%s", report.team_repos_source)
    return report.team_repos


def repos_for_team(mapping: dict[str, list[str]], team_focus: str) -> list[str]:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return unique_repos(mapping.values())
    if normalized == "it" and not mapping.get("it"):
        return unique_repos(mapping.values())
    return list(mapping.get(normalized, []))
