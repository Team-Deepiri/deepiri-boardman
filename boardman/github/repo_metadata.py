"""
Fetch repo metadata from GitHub API.

Signals are derived from the repo's file tree and name — zero hardcoded meaning.

Signal types:
  file:{filename}      — every file present in the repo (lowercased basename)
  dir:{dirname}        — every directory present (lowercased, one signal per unique name)
  lang:{language}      — primary language from GitHub API
  name:{token}         — repo name split on [-_.]  (single chars dropped)

These raw signals feed the IDF-based tier classifier. Nothing here assigns weights
or encodes domain knowledge — that emerges from frequency across the org's repos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from boardman.settings import settings


@dataclass
class RepoMetadata:
    full_name: str
    language: str = ""
    topics: List[str] = field(default_factory=list)   # kept for compat; not used for classification
    size_kb: int = 0
    default_branch: str = "main"
    raw_signals: List[str] = field(default_factory=list)  # file:X  dir:X  lang:X  name:X
    max_depth: int = 0
    top_level_dirs: List[str] = field(default_factory=list)
    signal_counts: dict[str, int] = field(default_factory=dict)


async def _fetch_file_tree_signals(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
    headers: dict,
) -> tuple[List[str], int, List[str], dict[str, int]]:
    """
    Call GitHub git/trees API (recursive) and emit one signal per unique
    file basename and directory name. No file content is read.
    Returns (signals, max_depth, top_level_dirs, signal_counts).
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    try:
        r = await client.get(url, headers=headers, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return [], 0, [], {}
        tree = r.json().get("tree", [])
    except Exception:
        return [], 0, [], {}

    signals: List[str] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    max_depth = 0
    top_level_dirs = set()

    for item in tree:
        path = (item.get("path") or "").lower()
        kind = item.get("type") or ""

        parts = path.split("/")
        depth = len(parts) - (1 if kind == "blob" else 0)
        if depth > max_depth:
            max_depth = depth

        if depth > 0:
            top_level_dirs.add(parts[0])

        if kind == "blob":
            # file signal — basename only
            basename = parts[-1]
            sig = f"file:{basename}"
            counts[sig] = counts.get(sig, 0) + 1
            if sig not in seen:
                seen.add(sig)
                signals.append(sig)

            # directory signals — every component of the path except the filename
            dir_parts = parts[:-1]
            for part in dir_parts:
                if not part:
                    continue
                sig = f"dir:{part}"
                counts[sig] = counts.get(sig, 0) + 1
                if sig not in seen:
                    seen.add(sig)
                    signals.append(sig)

    return signals, max_depth, sorted(list(top_level_dirs)), counts


def _name_signals(repo_full_name: str) -> List[str]:
    """Split repo name into raw tokens. No semantic mapping — IDF handles meaning."""
    name = repo_full_name.split("/")[-1].lower()
    tokens = [t for t in re.split(r"[-_.]", name) if len(t) > 1]
    return [f"name:{t}" for t in tokens]


def _parse_next_url(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if "; rel=" not in section:
            continue
        url_part, rel_part = section.split(";", 1)
        if 'rel="next"' in rel_part.replace(" ", ""):
            return url_part.strip().removeprefix("<").removesuffix(">")
    return None


async def fetch_repo_metadata(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
) -> Optional[RepoMetadata]:
    """Fetch language + size from GitHub API. Derive raw signals from file tree + repo name."""
    token = settings.github_pat
    if not token:
        return None

    gh_headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        r = await client.get(url, headers=gh_headers, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        branch = data.get("default_branch", "main") or "main"
        full_name = data.get("full_name", f"{owner}/{repo}")
        lang = (data.get("language") or "").strip()

        tree_signals, max_depth, top_level_dirs, signal_counts = await _fetch_file_tree_signals(
            client, owner, repo, branch, gh_headers
        )
        lang_signals = [f"lang:{lang.lower()}"] if lang else []
        name_sigs = _name_signals(full_name)

        return RepoMetadata(
            full_name=full_name,
            language=lang,
            topics=data.get("topics", []) or [],
            size_kb=data.get("size", 0),
            default_branch=branch,
            raw_signals=tree_signals + lang_signals + name_sigs,
            max_depth=max_depth,
            top_level_dirs=top_level_dirs,
            signal_counts=signal_counts,
        )
    except Exception:
        return None


async def fetch_repos_metadata(
    client: httpx.AsyncClient,
    repo_full_names: List[str],
) -> dict[str, RepoMetadata]:
    """Fetch metadata for multiple repos concurrently."""
    import asyncio

    async def fetch_one(fn: str):
        if "/" not in fn:
            return fn, None
        owner, repo = fn.split("/", 1)
        meta = await fetch_repo_metadata(client, owner, repo)
        return fn, meta

    completed = await asyncio.gather(
        *[fetch_one(fn) for fn in repo_full_names], return_exceptions=True
    )
    results: dict[str, RepoMetadata] = {}
    for item in completed:
        if isinstance(item, tuple):
            fn, meta = item
            if meta:
                results[fn] = meta
    return results
