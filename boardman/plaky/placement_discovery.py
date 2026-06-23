"""Resolve GitHub repo -> Plaky board_id + group_id via catalog fuzzy match + category fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from boardman.plaky.name_match import rank_plaky_rows
from boardman.plaky.plaky_catalog import (
    PlakyBoardEntry,
    PlakyCatalogCache,
    PlakyGroupEntry,
    get_plaky_catalog,
)
from boardman.plaky.repo_category import (
    DEFAULT_GROUP_NAME_QUERIES,
    infer_repo_category,
    plaky_board_query_for_category,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlacementResult:
    board_id: str
    group_id: str
    board_name: str
    group_name: str
    category: str
    source: str
    score: int


def _min_auto_score() -> int:
    return max(1, int(settings.plaky_placement_min_score or 400))


def _repo_slug(full_name: str, short_name: str = "") -> str:
    sn = (short_name or "").strip()
    if sn:
        return sn
    fn = (full_name or "").strip()
    if "/" in fn:
        return fn.split("/", 1)[1].strip()
    return fn


def _best_group_match(
    groups: List[PlakyGroupEntry],
    query: str,
    *,
    min_score: int,
) -> Tuple[Optional[PlakyGroupEntry], int]:
    rows = [{"id": g.id, "name": g.name} for g in groups]
    ranked, best = rank_plaky_rows(rows, query)
    if best and int(best.get("score") or 0) >= min_score:
        gid = str(best.get("id") or "")
        gname = str(best.get("name") or "")
        return PlakyGroupEntry(id=gid, name=gname), int(best["score"])
    if ranked and int(ranked[0].get("score") or 0) >= min_score:
        top = ranked[0]
        return PlakyGroupEntry(id=str(top["id"]), name=str(top.get("name") or "")), int(top["score"])
    return None, 0


def _best_board_match(boards: List[PlakyBoardEntry], query: str, *, min_score: int) -> Tuple[Optional[PlakyBoardEntry], int]:
    rows = [{"id": b.id, "name": b.name} for b in boards]
    ranked, best = rank_plaky_rows(rows, query)
    if best and int(best.get("score") or 0) >= min_score:
        bid = str(best.get("id") or "")
        for b in boards:
            if b.id == bid:
                return b, int(best["score"])
    if ranked and int(ranked[0].get("score") or 0) >= min_score:
        bid = str(ranked[0]["id"])
        for b in boards:
            if b.id == bid:
                return b, int(ranked[0]["score"])
    return None, 0


def _default_group_on_board(board: PlakyBoardEntry, repo_slug: str, *, min_score: int) -> Optional[PlakyGroupEntry]:
    g, score = _best_group_match(board.groups, repo_slug, min_score=min_score)
    if g:
        return g
    for q in DEFAULT_GROUP_NAME_QUERIES:
        g, score = _best_group_match(board.groups, q, min_score=min_score)
        if g:
            return g
    if board.groups:
        g0 = board.groups[0]
        return PlakyGroupEntry(id=g0.id, name=g0.name)
    return None


def discover_placement_from_catalog(
    catalog: PlakyCatalogCache,
    full_name: str,
    short_name: str = "",
    *,
    description: str = "",
) -> Optional[PlacementResult]:
    """Pure function for tests: repo slug -> group globally, else category -> board -> group."""
    slug = _repo_slug(full_name, short_name)
    if not slug:
        return None
    min_score = _min_auto_score()
    boards = catalog.boards

    best_global: Optional[Tuple[PlakyBoardEntry, PlakyGroupEntry, int]] = None
    for board in boards:
        g, score = _best_group_match(board.groups, slug, min_score=min_score)
        if not g:
            continue
        if best_global is None or score > best_global[2]:
            best_global = (board, g, score)

    if best_global:
        board, group, score = best_global
        category = infer_repo_category(slug, description)
        return PlacementResult(
            board_id=board.id,
            group_id=group.id,
            board_name=board.name,
            group_name=group.name,
            category=category,
            source="group_slug_match",
            score=score,
        )

    category = infer_repo_category(slug, description)
    board_query = plaky_board_query_for_category(category)
    board, b_score = _best_board_match(boards, board_query, min_score=min_score)
    if not board:
        _log.warning(
            "plaky placement: no board match for repo=%r category=%r query=%r",
            slug,
            category,
            board_query,
        )
        return None

    group = _default_group_on_board(board, slug, min_score=min_score)
    if not group:
        _log.warning("plaky placement: board %r has no groups for repo=%r", board.name, slug)
        return None

    return PlacementResult(
        board_id=board.id,
        group_id=group.id,
        board_name=board.name,
        group_name=group.name,
        category=category,
        source="category_board_fallback",
        score=b_score,
    )


async def resolve_placement_for_repo(
    full_name: str,
    short_name: str = "",
    *,
    description: str = "",
    force_catalog_refresh: bool = False,
) -> Optional[PlacementResult]:
    if not settings.plaky_placement_auto_discover:
        return None
    if not (settings.plaky_api_key or "").strip():
        _log.debug("plaky placement: skipped (PLAKY_API_KEY missing)")
        return None
    try:
        catalog, cache_label = await get_plaky_catalog(force=force_catalog_refresh)
    except Exception as exc:
        _log.warning("plaky placement: catalog unavailable for %r: %s", full_name, exc)
        return None
    result = discover_placement_from_catalog(catalog, full_name, short_name, description=description)
    if result:
        _log.info(
            "plaky placement: %r -> board=%r group=%r (%s, cache=%s, score=%s)",
            _repo_slug(full_name, short_name),
            result.board_name,
            result.group_name,
            result.source,
            cache_label,
            result.score,
        )
    return result
