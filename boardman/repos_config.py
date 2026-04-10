"""Load repos.yml for Plaky table routing."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from boardman.settings import settings


@dataclass(frozen=True)
class RepoRouting:
    category: str
    plaky_table: str
    description: str = ""


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


def get_routing(full_name: str, short_name: str, org: str) -> Optional[RepoRouting]:
    """Look up routing by full_name, then org/short_name."""
    raw = _load_raw()
    repos: Dict[str, Any] = raw.get("repos") or {}
    entry = repos.get(full_name) or repos.get(f"{org}/{short_name}")
    if not entry or not isinstance(entry, dict):
        return None
    return RepoRouting(
        category=str(entry.get("category", "")),
        plaky_table=str(entry.get("plaky_table", "")),
        description=str(entry.get("description", "")),
    )


def list_registered_repos() -> Dict[str, RepoRouting]:
    raw = _load_raw()
    out: Dict[str, RepoRouting] = {}
    for key, entry in (raw.get("repos") or {}).items():
        if isinstance(entry, dict):
            out[str(key)] = RepoRouting(
                category=str(entry.get("category", "")),
                plaky_table=str(entry.get("plaky_table", "")),
                description=str(entry.get("description", "")),
            )
    return out


def upsert_repo(key: str, category: str, plaky_table: str, description: str = "") -> None:
    path = _resolve_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {"repos": {}}
    if "repos" not in raw:
        raw["repos"] = {}
    raw["repos"][key] = {
        "category": category,
        "plaky_table": plaky_table,
        "description": description,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    reload_repos_config()
