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

import httpx

from boardman.github.read_cache import cached
from boardman.github.repo_fetch import github_request
from boardman.settings import settings


@dataclass
class RepoMetadata:
    full_name: str
    description: str = ""
    language: str = ""
    topics: list[str] = field(default_factory=list)  # kept for compat; not used for classification
    size_kb: int = 0
    default_branch: str = "main"
    raw_signals: list[str] = field(default_factory=list)  # file:X  dir:X  lang:X  name:X
    max_depth: int = 0
    top_level_dirs: list[str] = field(default_factory=list)
    important_paths: list[str] = field(default_factory=list)
    signal_counts: dict[str, int] = field(default_factory=dict)
    pushed_at: str = ""


async def fetch_repo_identity(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
) -> dict | None:
    """Fetch/cache the cheap repository identity response.

    Metadata, default-branch discovery, direction lookup, and hotspot analysis all need
    this response. Sharing it prevents one planning turn from paying the same GitHub GET
    several times.
    """

    async def _fetch() -> dict | None:
        try:
            response = await github_request(client, f"/repos/{owner}/{repo}")
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - graceful degradation
            return None

    return await cached(
        f"repo-identity:{owner}/{repo}",
        _fetch,
        # A successful GitHub response may omit optional fields in fixtures or for
        # enterprise-compatible APIs. The HTTP status is the failure signal here;
        # metadata callers already provide safe defaults for missing identity fields.
        ok=lambda value: isinstance(value, dict) and bool(value),
    )


async def fetch_repo_tree(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
) -> dict | None:
    """Fetch/cache one recursive tree payload shared by metadata and hotspot readers."""

    async def _fetch() -> dict | None:
        try:
            response = await github_request(
                client,
                f"/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - graceful degradation
            return None

    return await cached(
        f"repo-tree:{owner}/{repo}@{branch}",
        _fetch,
        ok=lambda value: isinstance(value, dict) and isinstance(value.get("tree"), list),
    )


async def _fetch_file_tree_signals(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    branch: str,
) -> tuple[list[str], int, list[str], dict[str, int]]:
    """
    Call GitHub git/trees API (recursive) and emit one signal per unique
    file basename and directory name. No file content is read.
    Returns (signals, max_depth, top_level_dirs, signal_counts).
    """
    try:
        data = await fetch_repo_tree(client, owner, repo, branch)
        if not data:
            return [], 0, [], {}
        tree = data.get("tree", [])
    except Exception:  # noqa: BLE001 - graceful degradation
        return [], 0, [], {}

    signals: list[str] = []
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


def _name_signals(repo_full_name: str) -> list[str]:
    """Split repo name into raw tokens. No semantic mapping — IDF handles meaning."""
    name = repo_full_name.split("/")[-1].lower()
    tokens = [t for t in re.split(r"[-_.]", name) if len(t) > 1]
    return [f"name:{t}" for t in tokens]


def _parse_next_url(link_header: str | None) -> str | None:
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
) -> RepoMetadata | None:
    """Fetch language + size from GitHub API. Derive raw signals from file tree + repo name."""
    token = settings.github_pat
    if not token:
        return None

    try:
        data = await fetch_repo_identity(client, owner, repo)
        if not data:
            return None
        branch = data.get("default_branch", "main") or "main"
        full_name = data.get("full_name", f"{owner}/{repo}")
        lang = (data.get("language") or "").strip()

        tree_signals, max_depth, top_level_dirs, signal_counts = await _fetch_file_tree_signals(
            client, owner, repo, branch
        )
        tree_data = await fetch_repo_tree(client, owner, repo, branch)
        important_paths: list[str] = []
        important_basenames = {
            "agents.md",
            "direction.md",
            "dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "makefile",
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "go.mod",
            "cargo.toml",
            "pom.xml",
            "readme.md",
        }
        for item in (tree_data or {}).get("tree", []):
            path = str(item.get("path") or "")
            base = path.rsplit("/", 1)[-1].casefold()
            if (
                base in important_basenames or path.casefold().startswith(".github/workflows/")
            ) and len(important_paths) < 40:
                important_paths.append(path)
        lang_signals = [f"lang:{lang.lower()}"] if lang else []
        name_sigs = _name_signals(full_name)

        return RepoMetadata(
            full_name=full_name,
            description=str(data.get("description") or ""),
            language=lang,
            topics=data.get("topics", []) or [],
            size_kb=data.get("size", 0),
            default_branch=branch,
            raw_signals=tree_signals + lang_signals + name_sigs,
            max_depth=max_depth,
            top_level_dirs=top_level_dirs,
            important_paths=important_paths,
            signal_counts=signal_counts,
            pushed_at=str(data.get("pushed_at") or ""),
        )
    except Exception:  # noqa: BLE001 - graceful degradation
        return None


async def fetch_repos_metadata(
    client: httpx.AsyncClient,
    repo_full_names: list[str],
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
