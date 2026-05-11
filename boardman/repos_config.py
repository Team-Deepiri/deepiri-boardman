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
    entry = yaml_map.get(full_name)
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


def get_routing(full_name: str, short_name: str, org: str) -> Optional[RepoRouting]:
    """Look up routing by full_name, then org/short_name; then org default."""
    return get_routing_with_source(full_name, short_name, org)[0]


def get_routing_with_source(full_name: str, short_name: str, org: str) -> tuple[Optional[RepoRouting], str]:
    """
    Resolve routing and tell caller if it came from explicit repo config or org defaults.
    source is one of: explicit | org_default | none
    """
    raw = _load_raw()
    repos: Dict[str, Any] = raw.get("repos") or {}
    entry = repos.get(full_name) or repos.get(f"{org}/{short_name}")
    if entry and isinstance(entry, dict):
        r = _parse_entry(entry)
        if r and _is_meaningful(r):
            return r, "explicit"
    owner = full_name.split("/")[0] if "/" in full_name else ""
    if owner == org:
        d = _defaults_routing()
        if d and _is_meaningful(d):
            return d, "org_default"
    return None, "none"


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
        if key not in raw["repos"]:
            raw["repos"][key] = {}
        if tier > 0:
            raw["repos"][key]["tier"] = tier
        elif "tier" in raw["repos"][key]:
            del raw["repos"][key]["tier"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    reload_repos_config()
