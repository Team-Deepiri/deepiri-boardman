"""Resolve GitHub repo → Plaky board_id + group_id (auto-discovery, no repos.yml).

Algorithm (``discover_placement_from_catalog``):
  Scan every group on Devin's five categorical boards; fuzzy-match the repo slug
  to a group name (``rank_plaky_rows``, min score from ``PLAKY_PLACEMENT_MIN_SCORE``).
  Highest score wins globally (e.g. ``deepiri-boardman`` → Bots / ``deepiri-boardman``).

  Boards use one Plaky group per repo. If no group matches, return None — do not
  fall back to Backlog, Open PRs, or another repo's group.

Entry point for webhooks: ``resolve_placement_for_repo`` → ``get_routing_async`` in repos_config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from boardman.plaky.name_match import rank_plaky_rows
from boardman.plaky.plaky_catalog import (
    PlakyBoardEntry,
    PlakyCatalogCache,
    PlakyGroupEntry,
    filter_categorical_boards,
    get_plaky_catalog,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlacementResult:
    """Resolved Plaky placement for one GitHub repo slug."""

    board_id: str
    group_id: str
    board_name: str
    group_name: str
    category: str  # Plaky board display name (from catalog; same as board_name)
    source: str  # group_slug_match
    score: int  # fuzzy match score from rank_plaky_rows


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


def discover_placement_from_catalog(
    catalog: PlakyCatalogCache,
    full_name: str,
    short_name: str = "",
    *,
    description: str = "",
) -> Optional[PlacementResult]:
    """Pure resolver for tests; same logic as live path without API/cache I/O."""
    slug = _repo_slug(full_name, short_name)
    if not slug:
        return None
    min_score = _min_auto_score()
    # Re-filter in case catalog was loaded from an older cache that included legacy boards.
    boards = filter_categorical_boards(catalog.boards)

    best_global: Optional[Tuple[PlakyBoardEntry, PlakyGroupEntry, int]] = None
    for board in boards:
        g, score = _best_group_match(board.groups, slug, min_score=min_score)
        if not g:
            continue
        if best_global is None or score > best_global[2]:
            best_global = (board, g, score)

    if not best_global:
        _log.warning(
            "plaky placement: no Plaky group matches repo slug %r "
            "(add a group named like the repo on the correct categorical board)",
            slug,
        )
        return None

    board, group, score = best_global
    return PlacementResult(
        board_id=board.id,
        group_id=group.id,
        board_name=board.name,
        group_name=group.name,
        category=board.name,
        source="group_slug_match",
        score=score,
    )


async def resolve_placement_for_repo(
    full_name: str,
    short_name: str = "",
    *,
    description: str = "",
    force_catalog_refresh: bool = False,
) -> Optional[PlacementResult]:
    """Load catalog (cached or live) and resolve placement; used by webhook handlers."""
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
