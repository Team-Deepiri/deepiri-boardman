from __future__ import annotations

import json
import logging
from pathlib import Path

from boardman.settings import settings

log = logging.getLogger(__name__)

TEAM_CHOICES = ("ai-ml", "qa", "frontend-backend-infra", "it", "all-teams")

DEFAULT_TEAM_REPOS: dict[str, list[str]] = {
    "ai-ml": ["deepiri-modelkit", "diri-cyrex"],
    "qa": ["deepiri-platform"],
    "frontend-backend-infra": [
        "deepiri-platform",
        "deepiri-core-api",
        "deepiri-api-gateway",
    ],
    "it": [],
}


def load_team_repos(path: str | Path | None = None) -> dict[str, list[str]]:
    """Load team → repo name mapping. Falls back to built-in defaults."""
    file_path = Path(path or settings.planning_team_repos_file)
    if not file_path.exists():
        log.debug("team_repos_file_missing path=%s using_defaults", file_path)
        return _with_derived_teams(DEFAULT_TEAM_REPOS.copy())
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("team_repos_load_failed path=%s error=%s using_defaults", file_path, exc)
        return _with_derived_teams(DEFAULT_TEAM_REPOS.copy())
    if not isinstance(raw, dict):
        log.warning("team_repos_invalid_root path=%s using_defaults", file_path)
        return _with_derived_teams(DEFAULT_TEAM_REPOS.copy())
    mapping: dict[str, list[str]] = {}
    for team, repos in raw.items():
        if isinstance(repos, list):
            mapping[str(team)] = [str(r).strip() for r in repos if str(r).strip()]
    if not mapping:
        return _with_derived_teams(DEFAULT_TEAM_REPOS.copy())
    return _with_derived_teams(mapping)


def repos_for_team(mapping: dict[str, list[str]], team_focus: str) -> list[str]:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return _unique_repos(mapping.values())
    if normalized == "it" and not mapping.get("it"):
        return _unique_repos(mapping.values())
    return list(mapping.get(normalized, []))


def _with_derived_teams(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    all_repos = _unique_repos(mapping.values())
    mapping.setdefault("all-teams", all_repos)
    if not mapping.get("it"):
        mapping["it"] = all_repos
    return mapping


def _unique_repos(repo_lists: object) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for repos in repo_lists:
        if not isinstance(repos, list):
            continue
        for name in repos:
            key = str(name).strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered
