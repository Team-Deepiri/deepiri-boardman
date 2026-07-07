"""Map GitHub repo slugs to Plaky board/group placement (no repos.yml IDs).

Used by ``placement_discovery`` when ``PLAKY_PLACEMENT_AUTO_DISCOVER`` is on.
Replaces manual ``repos.yml`` board/group IDs for webhook-driven task creation.

Resolution (see ``discover_placement_from_catalog``):
  Fuzzy-match repo slug against Plaky *group* names on categorical boards
  (``rank_plaky_rows``, min score from ``PLAKY_PLACEMENT_MIN_SCORE``). Highest
  score wins globally (e.g. ``deepiri-boardman`` → group ``deepiri-boardman`` on Bots).

  Devin's boards use one group per repo — there is no Backlog/Open PRs bucket.
  If no group matches, placement fails (returns None) so tasks are not dropped
  into another repo's group. Add the repo as a Plaky group first.

Category slug → Plaky board (used for metadata / future tooling; not a group fallback):
  platform       → Deepiri Platform + Services
  ai-runtime     → Bots
  dx             → Developer Tool Repos
  creative       → Creative Repos
  infra, unknown → Miscellaneous

Hint tuples are adapted from deepiri-axiom ``ecosystem/github_catalog.py`` (PR #9).
"""

from __future__ import annotations

from typing import Final

# Exact Plaky board titles for Devin's categorical layout (not legacy sprint/task boards).
PLAKY_BOARD_PLATFORM: Final[str] = "Deepiri Platform + Services"
PLAKY_BOARD_BOTS: Final[str] = "Bots"
PLAKY_BOARD_DEV_TOOLS: Final[str] = "Developer Tool Repos"
PLAKY_BOARD_CREATIVE: Final[str] = "Creative Repos"
PLAKY_BOARD_MISC: Final[str] = "Miscellaneous"

# Substrings matched against "repo-slug description" (case-insensitive) for step 2 fallback.
_PLATFORM_HINTS: Final[tuple[str, ...]] = (
    "api-gateway",
    "core-api",
    "auth-service",
    "web-frontend",
    "landing",
    "bridge",
    "language-intelligence",
    "shared-utils",
    "synapse",
)
_AI_HINTS: Final[tuple[str, ...]] = (
    "cyrex",
    "persola",
    "helox",
    "modelkit",
    "prismpipe",
    "training",
    "dataset",
    "agent-",
    "ollama",
    "aarflingo",
    "tombstone",
)
_INFRA_HINTS: Final[tuple[str, ...]] = (
    "vizult",
    "cascade",
    "conduit",
    "wooven",
    "axiom",
    "gpu",
    "zepgpu",
    "sugar-glider",
    "pkg-version",
    "memorymesh",
    "logger",
)
_DX_HINTS: Final[tuple[str, ...]] = (
    "sorge",
    "norozo",
    "boardman",
    "huddle",
    "polylogue",
    "demo",
)

CATEGORY_TO_PLAKY_BOARD: Final[dict[str, str]] = {
    "platform": PLAKY_BOARD_PLATFORM,
    "ai-runtime": PLAKY_BOARD_BOTS,
    "dx": PLAKY_BOARD_DEV_TOOLS,
    "creative": PLAKY_BOARD_CREATIVE,
    "infra": PLAKY_BOARD_MISC,
    "unknown": PLAKY_BOARD_MISC,
}

# Six category slugs above map to these five Plaky boards (infra + unknown → Miscellaneous).
PLAKY_CATEGORICAL_BOARD_NAMES: Final[frozenset[str]] = frozenset(CATEGORY_TO_PLAKY_BOARD.values())


def infer_repo_category(name: str, description: str = "") -> str:
    """Return axiom-style category slug: platform | ai-runtime | infra | dx | creative | unknown."""
    n = (name or "").strip().lower()
    d = (description or "").strip().lower()
    blob = f"{n} {d}"
    if n == "deepiri-platform" or "monorepo" in d:
        return "platform"
    if n.startswith("diri-") or any(h in blob for h in _AI_HINTS):
        return "ai-runtime"
    if any(h in blob for h in _PLATFORM_HINTS):
        return "platform"
    if any(h in blob for h in _INFRA_HINTS):
        return "infra"
    if any(h in blob for h in _DX_HINTS):
        return "dx"
    if n.startswith("deepiri-") or n.startswith("diri-"):
        return "creative"
    return "unknown"


def plaky_board_query_for_category(category: str) -> str:
    """Plaky board name to pass to rank_plaky_rows for board-level fallback."""
    cat = (category or "").strip().lower()
    return CATEGORY_TO_PLAKY_BOARD.get(cat) or PLAKY_BOARD_MISC


def is_categorical_plaky_board(name: str) -> bool:
    """True when `name` is one of Devin's five categorical Plaky boards."""
    return (name or "").strip() in PLAKY_CATEGORICAL_BOARD_NAMES


def categorical_plaky_board_names() -> tuple[str, ...]:
    """Stable tuple of allowed board display names (for docs/tests)."""
    return tuple(sorted(PLAKY_CATEGORICAL_BOARD_NAMES))
