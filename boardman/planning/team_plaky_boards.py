from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from boardman.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlakyBoardRef:
    board_id: str
    space_id: str = ""


def load_team_plaky_boards(path: str | Path | None = None) -> dict[str, PlakyBoardRef]:
    file_path = Path(path or settings.planning_team_plaky_boards_file)
    if not file_path.exists():
        log.debug("team_plaky_boards_file_missing path=%s", file_path)
        return {}
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("team_plaky_boards_load_failed path=%s error=%s", file_path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("team_plaky_boards_invalid_root path=%s", file_path)
        return {}
    mapping: dict[str, PlakyBoardRef] = {}
    for team, value in raw.items():
        ref = _parse_board_ref(value)
        if ref is not None:
            mapping[str(team)] = ref
    return _with_derived_teams(mapping)


def board_for_team(mapping: dict[str, PlakyBoardRef], team_focus: str) -> PlakyBoardRef | None:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return mapping.get("all-teams") or _first_board(mapping)
    if normalized == "it" and "it" not in mapping:
        return mapping.get("all-teams") or _first_board(mapping)
    return mapping.get(normalized)


def boards_for_team(
    mapping: dict[str, PlakyBoardRef],
    team_focus: str,
) -> list[PlakyBoardRef]:
    normalized = team_focus.strip().lower()
    if normalized in {"all-teams", "all", "engineering"}:
        return _unique_boards(mapping.values())
    if normalized == "it" and "it" not in mapping:
        return _unique_boards(mapping.values())
    ref = mapping.get(normalized)
    return [ref] if ref else []


def _parse_board_ref(value: object) -> PlakyBoardRef | None:
    if not isinstance(value, dict):
        return None
    board_id = str(value.get("board_id") or value.get("boardId") or "").strip()
    if not board_id:
        return None
    space_id = str(value.get("space_id") or value.get("spaceId") or "").strip()
    return PlakyBoardRef(board_id=board_id, space_id=space_id)


def _with_derived_teams(mapping: dict[str, PlakyBoardRef]) -> dict[str, PlakyBoardRef]:
    if mapping and "all-teams" not in mapping:
        first = _first_board(mapping)
        if first is not None:
            mapping["all-teams"] = first
    if mapping and "it" not in mapping and "all-teams" in mapping:
        mapping["it"] = mapping["all-teams"]
    return mapping


def _first_board(mapping: dict[str, PlakyBoardRef]) -> PlakyBoardRef | None:
    for key in ("ai-ml", "qa", "frontend-backend-infra"):
        if key in mapping:
            return mapping[key]
    return next(iter(mapping.values()), None)


def _unique_boards(boards: object) -> list[PlakyBoardRef]:
    seen: set[str] = set()
    ordered: list[PlakyBoardRef] = []
    for board in boards:
        if not isinstance(board, PlakyBoardRef):
            continue
        if board.board_id not in seen:
            seen.add(board.board_id)
            ordered.append(board)
    return ordered
