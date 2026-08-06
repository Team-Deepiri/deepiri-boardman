"""Fetch and cache Plaky boards + groups for repo → placement auto-discovery.

Pattern mirrors deepiri-axiom org-catalog cache: list all boards, fetch groups per board,
persist to ``.boardman/plaky-catalog.json`` with a 24h TTL (``PLAKY_CATALOG_TTL_SECONDS``).

Board filtering (``PLAKY_CATALOG_CATEGORICAL_ONLY``, default true)
-----------------------------------------------------------------
Repo placement assumes Devin's layout: **one Plaky group per GitHub repo**, named like the
repo slug (``deepiri-*`` / ``diri-*``). Board *titles* are not hardcoded — new boards
(e.g. AI / ML Libraries) are picked up automatically when their groups look like repos.

When categorical-only is on, ``filter_categorical_boards`` keeps boards that pass
``is_categorical_board`` and drops:

- Legacy sprint/kanban boards whose groups are buckets (Backlog, Open PRs, …) — see
  ``_SPRINT_GROUP_NAMES``.
- Single-group test boards (e.g. group ``Boardman`` on ``Boardman Test Board``).

Empty boards and boards with no repo-like groups are also excluded. Heuristic assumptions
and edge cases are documented on ``is_categorical_board``.

Consumers: ``placement_discovery.resolve_placement_for_repo`` (webhooks via ``get_routing_async``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from boardman.plaky.client import PlakyClient
from boardman.settings import settings

_log = logging.getLogger(__name__)

CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 86_400  # 24h

# Observed Plaky / kanban bucket labels on legacy sprint boards (not repo-slug groups).
# Sourced from Deepiri's old sprint/task boards (AI Task Board, etc.) and common Plaky
# defaults. Extend this set if Plaky or the team adds new sprint-style group names —
# otherwise those boards may be misclassified as repo-catalog boards.
_SPRINT_GROUP_NAMES: frozenset[str] = frozenset(
    {
        "backlog",
        "open prs",
        "in progress",
        "in review",
        "done",
        "qa",
        "testing",
        "blocked",
        "todo",
        "to do",
        "icebox",
        "ready",
        "review",
        "merged",
        "closed",
        "archive",
        "archived",
    }
)


def _normalize_group_name(name: str) -> str:
    """Lowercase slug; strip leading emoji/punctuation (e.g. Plaky ``‼️deepiri-platform``)."""
    n = (name or "").strip().casefold()
    i = 0
    while i < len(n) and not n[i].isalnum():
        i += 1
    return n[i:]


def looks_like_repo_group(name: str) -> bool:
    """True when a Plaky group name looks like a GitHub repo slug, not a sprint bucket."""
    n = _normalize_group_name(name)
    if not n or " " in n:
        return False
    if n in _SPRINT_GROUP_NAMES:
        return False
    if n.startswith("deepiri-") or n.startswith("diri-"):
        return True
    # Short slugs without org prefix (e.g. ``diva`` on Creative Repos).
    return len(n) >= 2 and n.replace("-", "").replace("_", "").isalnum()


def _group_structure_counts(board: PlakyBoardEntry) -> tuple[int, int, int]:
    """Return (repo_like, sprint_like, prefixed_repo) counts for heuristic logging."""
    repo_like = 0
    sprint_like = 0
    prefixed_repo = 0
    for group in board.groups:
        norm = _normalize_group_name(group.name)
        if norm in _SPRINT_GROUP_NAMES:
            sprint_like += 1
        elif looks_like_repo_group(group.name):
            repo_like += 1
            if norm.startswith("deepiri-") or norm.startswith("diri-"):
                prefixed_repo += 1
    return repo_like, sprint_like, prefixed_repo


def is_categorical_board(board: PlakyBoardEntry) -> bool:
    """Detect repo-catalog boards from group structure (no hardcoded board titles).

    Assumptions (Devin / Deepiri Plaky layout):
      - Repo-catalog boards use one group per repo, named like the GitHub slug
        (``deepiri-*``, ``diri-*``, or a short alphanumeric slug such as ``diva``).
      - Legacy sprint boards use kanban buckets listed in ``_SPRINT_GROUP_NAMES``.
      - Boards are not mixed: a catalog board should not also carry Backlog/Open PRs
        groups. If that ever appears, treat it as a Plaky layout mistake to fix, not
        something Boardman invents a special case for.

    Decision:
      - Reject empty boards and boards with only sprint buckets (no repo-like groups).
      - Reject single-group boards unless that group is a ``deepiri-`` / ``diri-`` slug
        (filters ad-hoc test boards like ``Boardman`` on ``Boardman Test Board``).
      - Accept if there is at least one prefixed repo group, or two+ repo-like groups.

    Limitations: naming conventions can change; if Plaky introduces new sprint bucket
    labels, update ``_SPRINT_GROUP_NAMES``. ``filter_categorical_boards`` logs kept/dropped
    board names; mixed sprint+repo boards log a warning (layout smell).
    """
    if not board.groups:
        return False
    repo_like, sprint_like, prefixed_repo = _group_structure_counts(board)
    # Unexpected in Devin's layout — flag for operators; still classify by repo groups.
    if sprint_like > 0 and repo_like > 0:
        _log.warning(
            "plaky catalog: board %r has mixed sprint buckets (%s) and repo-like groups (%s); "
            "expected one group per repo only — fix in Plaky if this is unintentional",
            board.name,
            sprint_like,
            repo_like,
        )
    if sprint_like > 0 and repo_like == 0:
        return False
    if repo_like == 0:
        return False
    # Exclude single-group test boards (e.g. group ``Boardman`` on ``Boardman Test Board``).
    if len(board.groups) == 1 and prefixed_repo == 0:
        return False
    return prefixed_repo >= 1 or repo_like >= 2


def filter_categorical_boards(boards: List[PlakyBoardEntry]) -> List[PlakyBoardEntry]:
    """Keep repo-catalog boards; drop legacy sprint/test boards when categorical-only is on."""
    if not settings.plaky_catalog_categorical_only:
        return boards
    kept: List[PlakyBoardEntry] = []
    dropped_names: List[str] = []
    for board in boards:
        if is_categorical_board(board):
            kept.append(board)
        else:
            dropped_names.append(board.name or board.id)
    if dropped_names:
        _log.info(
            "plaky catalog: scoped to %s repo-catalog board(s) (%s legacy/test board(s) excluded): %s",
            len(kept),
            len(dropped_names),
            ", ".join(dropped_names),
        )
    if kept:
        _log.debug(
            "plaky catalog: keeping repo-catalog board(s): %s",
            ", ".join(b.name or b.id for b in kept),
        )
    # Heuristic sanity: dropping almost everything usually means Plaky layout changed.
    if boards and len(kept) == 0:
        _log.warning(
            "plaky catalog: categorical filter kept 0 of %s board(s) — "
            "check Plaky group naming or set PLAKY_CATALOG_CATEGORICAL_ONLY=false",
            len(boards),
        )
    elif boards and len(dropped_names) >= max(3, len(boards) // 2):
        _log.warning(
            "plaky catalog: categorical filter dropped %s of %s board(s) (%s) — "
            "verify heuristic still matches live Plaky layout",
            len(dropped_names),
            len(boards),
            ", ".join(dropped_names),
        )
    return kept


@dataclass
class PlakyGroupEntry:
    id: str
    name: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Optional[PlakyGroupEntry]:
        if not isinstance(row, dict):
            return None
        gid = str(row.get("id") or row.get("groupId") or row.get("group_id") or "").strip()
        name = str(row.get("name") or row.get("title") or "").strip()
        if not gid:
            return None
        return cls(id=gid, name=name or gid)


@dataclass
class PlakyBoardEntry:
    id: str
    name: str
    space_id: str = ""
    groups: List[PlakyGroupEntry] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any], groups: Optional[List[PlakyGroupEntry]] = None) -> Optional[PlakyBoardEntry]:
        if not isinstance(row, dict):
            return None
        bid = str(row.get("id") or "").strip()
        if not bid:
            return None
        return cls(
            id=bid,
            name=str(row.get("name") or row.get("title") or "").strip() or bid,
            space_id=str(row.get("space_id") or row.get("spaceId") or "").strip(),
            groups=list(groups or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "space_id": self.space_id,
            "groups": [{"id": g.id, "name": g.name} for g in self.groups],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional[PlakyBoardEntry]:
        if not isinstance(data, dict):
            return None
        bid = str(data.get("id") or "").strip()
        if not bid:
            return None
        groups = [
            PlakyGroupEntry(id=str(g["id"]), name=str(g.get("name") or g["id"]))
            for g in (data.get("groups") or [])
            if isinstance(g, dict) and str(g.get("id") or "").strip()
        ]
        return cls(
            id=bid,
            name=str(data.get("name") or "").strip() or bid,
            space_id=str(data.get("space_id") or "").strip(),
            groups=groups,
        )


@dataclass
class PlakyCatalogCache:
    fetched_at: float
    source: str
    boards: List[PlakyBoardEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "source": self.source,
            "boards": [b.to_dict() for b in self.boards],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional[PlakyCatalogCache]:
        if not isinstance(data, dict):
            return None
        boards_raw = data.get("boards") or []
        boards: List[PlakyBoardEntry] = []
        if isinstance(boards_raw, list):
            for row in boards_raw:
                if isinstance(row, dict):
                    b = PlakyBoardEntry.from_dict(row)
                    if b:
                        boards.append(b)
        return cls(
            fetched_at=float(data.get("fetched_at") or 0),
            source=str(data.get("source") or "unknown"),
            boards=boards,
        )


def catalog_cache_path() -> Path:
    p = Path(settings.plaky_catalog_cache_path or ".boardman/plaky-catalog.json")
    if p.is_absolute():
        return p
    return Path.cwd() / p


def load_cached_catalog() -> Optional[PlakyCatalogCache]:
    path = catalog_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return PlakyCatalogCache.from_dict(data)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("plaky catalog: could not read cache %s: %s", path, exc)
    return None


def save_catalog_cache(cache: PlakyCatalogCache) -> Path:
    path = catalog_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


async def _fetch_board_groups(client: PlakyClient, board_row: dict[str, Any]) -> Optional[PlakyBoardEntry]:
    bid = str(board_row.get("id") or "").strip()
    if not bid:
        return None
    gr = await client.list_groups(bid)
    groups: List[PlakyGroupEntry] = []
    if gr.get("ok"):
        for row in gr.get("groups") or []:
            g = PlakyGroupEntry.from_row(row if isinstance(row, dict) else {})
            if g:
                groups.append(g)
    return PlakyBoardEntry.from_row(board_row, groups)


async def fetch_live_catalog(client: Optional[PlakyClient] = None) -> tuple[PlakyCatalogCache, str]:
    """Pull all boards + groups from Plaky; scope to categorical boards before caching."""
    c = client or PlakyClient()
    boards_result = await c.list_boards()
    if not boards_result.get("ok"):
        msg = str(boards_result.get("message") or "list_boards failed")
        raise RuntimeError(msg)
    raw_boards = boards_result.get("boards") or []
    if not isinstance(raw_boards, list) or not raw_boards:
        raise RuntimeError("Plaky returned no boards")

    tasks = [_fetch_board_groups(c, row) for row in raw_boards if isinstance(row, dict)]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    boards: List[PlakyBoardEntry] = []
    for item in gathered:
        if isinstance(item, PlakyBoardEntry):
            boards.append(item)
        elif isinstance(item, Exception):
            _log.warning("plaky catalog: board groups fetch failed: %s", item)
    boards.sort(key=lambda b: b.name.casefold())
    boards = filter_categorical_boards(boards)
    now = time.time()
    return PlakyCatalogCache(fetched_at=now, source="plaky-api", boards=boards), "plaky-api"


async def refresh_plaky_catalog(
    *,
    force: bool = False,
    client: Optional[PlakyClient] = None,
) -> tuple[PlakyCatalogCache, str]:
    """Return catalog from disk if fresh; otherwise refresh from Plaky API (falls back to stale cache)."""
    ttl = float(settings.plaky_catalog_ttl_seconds or DEFAULT_TTL_SECONDS)
    cached = load_cached_catalog()
    now = time.time()
    if cached and not force and ttl > 0 and (now - cached.fetched_at) < ttl:
        return cached, f"cache:{cached.source}"

    try:
        live, source = await fetch_live_catalog(client)
        if live.boards:
            save_catalog_cache(live)
            return live, source
    except Exception as exc:
        _log.warning("plaky catalog: live fetch failed: %s", exc)
        if cached:
            return cached, f"stale-cache:{cached.source}"
        raise

    if cached:
        return cached, f"stale-cache:{cached.source}"
    raise RuntimeError("Plaky catalog unavailable and no cache on disk")


async def get_plaky_catalog(*, force: bool = False) -> tuple[PlakyCatalogCache, str]:
    """Primary entry for placement discovery."""
    return await refresh_plaky_catalog(force=force)
