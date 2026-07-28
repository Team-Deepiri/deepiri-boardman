from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class PlakyBoardRef:
    board_id: str
    space_id: str = ""


def parse_board_ref(value: object) -> PlakyBoardRef | None:
    if not isinstance(value, dict):
        return None
    board_id = str(value.get("board_id") or value.get("boardId") or "").strip()
    if not board_id:
        return None
    space_id = str(value.get("space_id") or value.get("spaceId") or "").strip()
    return PlakyBoardRef(board_id=board_id, space_id=space_id)


def with_derived_repo_teams(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    all_repos = unique_repos(mapping.values())
    mapping.setdefault("all-teams", all_repos)
    if not mapping.get("it"):
        mapping["it"] = all_repos
    return mapping


def with_derived_board_teams(mapping: dict[str, PlakyBoardRef]) -> dict[str, PlakyBoardRef]:
    if mapping and "all-teams" not in mapping:
        first = first_board(mapping)
        if first is not None:
            mapping["all-teams"] = first
    if mapping and "it" not in mapping and "all-teams" in mapping:
        mapping["it"] = mapping["all-teams"]
    return mapping


def unique_repos(repo_lists: object) -> list[str]:
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


def first_board(mapping: dict[str, PlakyBoardRef]) -> PlakyBoardRef | None:
    for key in ("ai-ml", "qa", "frontend-backend-infra"):
        if key in mapping:
            return mapping[key]
    return next(iter(mapping.values()), None)


def unique_boards(boards: object) -> list[PlakyBoardRef]:
    seen: set[str] = set()
    ordered: list[PlakyBoardRef] = []
    for board in boards:
        if not isinstance(board, PlakyBoardRef):
            continue
        if board.board_id not in seen:
            seen.add(board.board_id)
            ordered.append(board)
    return ordered
