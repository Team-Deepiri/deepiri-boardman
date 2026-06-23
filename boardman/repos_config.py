"""Load repos.yml for Plaky table routing; optionally merge with GitHub org repo list."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yaml

from boardman.settings import settings


@dataclass(frozen=True)
class RepoRouting:
    category: str = ""
    # Legacy label: Plaky "group" name in UI (for descriptions); API placement uses IDs below.
    plaky_table: str = ""
    plaky_board_id: str = ""
    plaky_group_id: str = ""
    description: str = ""
    tier: int = 0  # 0 = unclassified, 1/2/3 = QA tier


def _resolve_path() -> Path:
    p = Path(settings.repos_yml_path)
    if p.is_absolute():
        return p
    return Path.cwd() / p


@lru_cache
def _load_raw() -> Dict[str, Any]:
    path = _resolve_path()
    if not path.is_file():
        return {"repos": {}}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "repos" not in data:
        data["repos"] = {}
    return data


def reload_repos_config() -> None:
    _load_raw.cache_clear()


def repos_yaml_canonical_repo_key(identifier: str) -> str:
    """Key under repos.yml ``repos``: repository name segment only (no owner prefix)."""
    s = (identifier or "").strip()
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    return s


def routing_yaml_candidate_map_keys(full_name: str, short_repo_name: str = "", github_org: str = "") -> list[str]:
    """
    Lookup order for repos.yml entries. Supports canonical short keys plus legacy ``owner/repo`` keys.
    """
    fn = (full_name or "").strip()
    o = ((github_org or "").strip() or (settings.github_org or "").strip())
    bare = str(settings.github_bare_repo_owner or "").strip()

    inferred_short = ""
    if fn and "/" in fn:
        inferred_short = fn.split("/", 1)[1].strip()
    sn = ((short_repo_name or "").strip() or inferred_short or (fn if fn and "/" not in fn else ""))

    ordered: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        kk = k.strip()
        if kk and kk not in seen:
            seen.add(kk)
            ordered.append(kk)

    add(fn)
    if sn:
        if o:
            add(f"{o}/{sn}")
        if bare:
            add(f"{bare}/{sn}")
        add(sn)
    return ordered


def _parse_entry(entry: Any) -> Optional[RepoRouting]:
    if not isinstance(entry, dict):
        return None
    tier_val = entry.get("tier")
    tier = 0
    if tier_val is not None:
        try:
            tier = int(tier_val)
        except (ValueError, TypeError):
            tier = 0
    return RepoRouting(
        category=str(entry.get("category", "")),
        plaky_table=str(entry.get("plaky_table", "")),
        plaky_board_id=str(entry.get("plaky_board_id", "")),
        plaky_group_id=str(entry.get("plaky_group_id", "")),
        description=str(entry.get("description", "")),
        tier=tier,
    )


def _is_meaningful(r: RepoRouting) -> bool:
    return bool(
        r.plaky_table or r.category or r.description or r.plaky_board_id or r.plaky_group_id or r.tier > 0
    )


def team_assignment_field_sync_board_id() -> str:
    """Board id from repos.yml `defaults.plaky_board_id` (startup team_assignments field-key sync)."""
    r = _defaults_routing()
    if r and (r.plaky_board_id or "").strip():
        return r.plaky_board_id.strip()
    return ""


def _defaults_routing() -> Optional[RepoRouting]:
    raw = _load_raw()
    d = raw.get("defaults")
    if isinstance(d, dict):
        cat = str(d.get("category", "") or settings.default_repo_category or "")
        table = str(d.get("plaky_table", "") or settings.default_plaky_table or "")
        bid = str(d.get("plaky_board_id", "") or "")
        gid = str(d.get("plaky_group_id", "") or "")
        desc = str(d.get("description", "") or "")
    else:
        cat = str(settings.default_repo_category or "")
        table = str(settings.default_plaky_table or "")
        bid = ""
        gid = ""
        desc = ""
    if not cat and not table and not desc and not bid and not gid:
        return None
    return RepoRouting(
        category=cat, plaky_table=table, plaky_board_id=bid, plaky_group_id=gid, description=desc
    )


def _routing_for_full_name(full_name: str, yaml_map: Dict[str, Any], org: str) -> RepoRouting:
    short_repo = full_name.split("/", 1)[1] if "/" in full_name else full_name
    for key in routing_yaml_candidate_map_keys(full_name, short_repo, org):
        entry = yaml_map.get(key)
        if entry and isinstance(entry, dict):
            r = _parse_entry(entry)
            if r and _is_meaningful(r):
                return r
    owner = full_name.split("/")[0] if "/" in full_name else ""
    if owner == org:
        d = _defaults_routing()
        if d and _is_meaningful(d):
            return d
    return RepoRouting()


def get_routing(full_name: str, short_name: str, org: str, with_source: bool = False) -> Any:
    """Look up routing by full_name, then org/short_name; optionally include the source."""
    raw = _load_raw()
    repos: Dict[str, Any] = raw.get("repos") or {}
    for key in routing_yaml_candidate_map_keys(full_name, short_name, org):
        entry = repos.get(key)
        if entry and isinstance(entry, dict):
            r = _parse_entry(entry)
            if r and _is_meaningful(r):
                return (r, "explicit") if with_source else r
    owner = full_name.split("/")[0] if "/" in full_name else ""
    if owner == org:
        d = _defaults_routing()
        if d and _is_meaningful(d):
            return (d, "org_default") if with_source else d
    return (None, "none") if with_source else None


async def get_routing_async(
    full_name: str,
    short_name: str,
    org: str,
    with_source: bool = False,
    *,
    description: str = "",
) -> Any:
    """
    Resolve Plaky board/group for a GitHub repo.

    When ``plaky_placement_auto_discover`` is enabled (default):
      - Loads cached Plaky catalog (boards + groups).
      - Fuzzy-matches repo slug → group, or falls back to category → board.
      - Does not read ``repos.yml`` for board_id / group_id.

    Set ``PLAKY_PLACEMENT_AUTO_DISCOVER=false`` to use legacy ``repos.yml`` routing only.
    """
    if settings.plaky_placement_auto_discover:
        from boardman.plaky.placement_discovery import resolve_placement_for_repo

        slug = (short_name or "").strip()
        if not slug and "/" in (full_name or ""):
            slug = full_name.split("/", 1)[1].strip()
        placement = await resolve_placement_for_repo(
            full_name,
            slug,
            description=description,
        )
        if placement:
            r = RepoRouting(
                category=placement.category,
                plaky_table=placement.group_name,
                plaky_board_id=placement.board_id,
                plaky_group_id=placement.group_id,
                description=f"discovered:{placement.source}",
            )
            src = f"discovered:{placement.source}"
            return (r, src) if with_source else r
        return (None, "discovered:none") if with_source else None

    return get_routing(full_name, short_name, org, with_source=with_source)


def list_registered_repos() -> Dict[str, RepoRouting]:
    """Repos declared in repos.yml only (sync)."""
    raw = _load_raw()
    out: Dict[str, RepoRouting] = {}
    for key, entry in (raw.get("repos") or {}).items():
        if isinstance(entry, dict):
            r = _parse_entry(entry)
            if r:
                out[str(key)] = r
    return out


async def list_workspace_repos(client: Optional[httpx.AsyncClient] = None) -> Dict[str, RepoRouting]:
    """Org repos from GitHub API merged with repos.yml; falls back to YAML-only without GITHUB_PAT."""
    yaml_map: Dict[str, Any] = dict(_load_raw().get("repos") or {})
    org = settings.github_org

    if not settings.github_pat:
        return list_registered_repos()

    from boardman.github.org_repos import fetch_org_repository_full_names

    close = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        close = True
    try:
        org_names = await fetch_org_repository_full_names(
            client,
            org,
            skip_archived=settings.github_skip_archived,
        )
        out: Dict[str, RepoRouting] = {}
        for fn in org_names:
            out[fn] = _routing_for_full_name(fn, yaml_map, org)
        for key, entry in yaml_map.items():
            if not isinstance(entry, dict):
                continue
            if key in out:
                continue
            r = _parse_entry(entry)
            if r and _is_meaningful(r):
                out[str(key)] = r
        return dict(sorted(out.items()))
    finally:
        if close and client is not None:
            await client.aclose()


def upsert_repo(key: str, category: str, plaky_table: str, description: str = "", tier: int = 0) -> None:
    path = _resolve_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {"repos": {}}
    if "repos" not in raw:
        raw["repos"] = {}
    key = repos_yaml_canonical_repo_key(key)
    raw["repos"][key] = {
        "category": category,
        "plaky_table": plaky_table,
        "description": description,
    }
    if tier > 0:
        raw["repos"][key]["tier"] = tier
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    reload_repos_config()


def update_repo_tiers(tier_map: dict[str, int]) -> None:
    """Batch update tier classifications for repos."""
    path = _resolve_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {"repos": {}}
    if "repos" not in raw:
        raw["repos"] = {}
    for key, tier in tier_map.items():
        nk = repos_yaml_canonical_repo_key(key)
        if nk not in raw["repos"]:
            raw["repos"][nk] = {}
        if tier > 0:
            raw["repos"][nk]["tier"] = tier
        elif "tier" in raw["repos"][nk]:
            del raw["repos"][nk]["tier"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    reload_repos_config()
