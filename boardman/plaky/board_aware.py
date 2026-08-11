"""Board-aware placement + person-field resolution for the shared category boards.

The 2026-06 Plaky redesign replaces per-repo boards with shared category boards
(Deepiri Platform + Services, Bots, Developer Tool Repos, Creative Repos,
Miscellaneous). On those boards each repo gets its own group whose name equals
the repo short name, and the person/status field keys DIFFER between boards
(e.g. Assignee is person-2 on one board and person-4 on another).

Helpers here resolve, for a given target board:
  - the repo's group by name, falling back to the configured/static group id
  - the engineer / QA person field keys from the board's own schema

Both are safe to call on legacy boards: no repo-named group means the fallback
group wins, and schema inference returns the same keys the static config holds.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from boardman.assignment.config import infer_plaky_field_keys_from_normalized
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.settings import settings

logger = logging.getLogger(__name__)

_groups_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _groups_ttl_seconds() -> float:
    ttl = getattr(settings, "plaky_board_schema_cache_ttl_seconds", 90)
    try:
        return max(0.0, float(ttl))
    except (TypeError, ValueError):
        return 90.0


def clear_group_cache() -> None:
    _groups_cache.clear()


async def _board_groups(board_id: str, plaky: PlakyClient | None = None) -> list[dict[str, Any]]:
    bid = (board_id or "").strip()
    if not bid:
        return []
    ttl = _groups_ttl_seconds()
    now = time.monotonic()
    cached = _groups_cache.get(bid)
    if cached and ttl > 0 and (now - cached[0]) < ttl:
        return cached[1]
    client = plaky or PlakyClient()
    try:
        res = await client.list_groups(bid)
    except Exception as exc:
        logger.warning("board_aware: list_groups(%s) failed: %s", bid, exc)
        return cached[1] if cached else []
    groups = res.get("groups") if isinstance(res, dict) and res.get("ok") else None
    if not isinstance(groups, list):
        return cached[1] if cached else []
    _groups_cache[bid] = (now, groups)
    return groups


async def resolve_group_for_repo(
    board_id: str | None,
    repo_short_name: str,
    fallback_group_id: str | None = None,
    *,
    plaky: PlakyClient | None = None,
) -> str | None:
    """Group id on ``board_id`` whose name equals the repo short name (case-insensitive).

    Returns ``fallback_group_id`` when the board has no group named after the repo,
    so legacy boards and not-yet-created groups keep their configured placement.
    """
    want = (repo_short_name or "").strip().casefold()
    bid = (board_id or "").strip()
    if bid and want:
        for g in await _board_groups(bid, plaky):
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip().casefold()
            gid = str(g.get("id") or g.get("_id") or "").strip()
            if name == want and gid:
                return gid
    return fallback_group_id


async def board_person_field_keys(board_id: str | None) -> dict[str, str] | None:
    """Engineer/QA person field keys from this board's own schema.

    Returns ``None`` when the schema could not be fetched (caller should fall back
    to the static team_assignments keys), or a dict that may contain ``engineer``
    and/or ``qa`` keys when the schema is known. An absent key on a known schema
    means the board has no matching person column — do not substitute the global
    key, it would target the wrong column.
    """
    bid = (board_id or "").strip()
    if not bid:
        return None
    try:
        bundle = await fetch_board_schema_bundle(bid)
    except Exception as exc:
        logger.warning("board_aware: schema fetch for board %s failed: %s", bid, exc)
        return None
    if not isinstance(bundle, dict) or not bundle.get("ok"):
        return None
    normalized = bundle.get("normalized")
    if not isinstance(normalized, dict) or not normalized.get("fields"):
        return None
    inferred = infer_plaky_field_keys_from_normalized(normalized)
    return {k: v for k, v in inferred.items() if k in ("engineer", "qa") and v}
