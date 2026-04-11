"""Fetch repo metadata from GitHub API (language, topics, size)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from boardman.settings import settings


@dataclass
class RepoMetadata:
    full_name: str
    language: str = ""
    topics: List[str] = field(default_factory=list)
    size_kb: int = 0
    default_branch: str = "main"


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
    """Fetch language, topics, size for a single repo."""
    token = settings.github_pat
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{owner}/{repo}"
    
    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        
        return RepoMetadata(
            full_name=data.get("full_name", f"{owner}/{repo}"),
            language=data.get("language", "") or "",
            topics=data.get("topics", []) or [],
            size_kb=data.get("size", 0),
            default_branch=data.get("default_branch", "main") or "main",
        )
    except Exception:
        return None


async def fetch_repos_metadata(
    client: httpx.AsyncClient,
    repo_full_names: List[str],
) -> dict[str, RepoMetadata]:
    """Fetch metadata for multiple repos. Parallel requests."""
    results: dict[str, RepoMetadata] = {}
    
    async def fetch_one(fn: str) -> tuple[str, Optional[RepoMetadata]]:
        if "/" not in fn:
            return fn, None
        owner, repo = fn.split("/", 1)
        meta = await fetch_repo_metadata(client, owner, repo)
        return fn, meta
    
    import asyncio
    tasks = [fetch_one(fn) for fn in repo_full_names]
    completed = await asyncio.gather(*tasks, return_exceptions=True)
    
    for item in completed:
        if isinstance(item, tuple):
            fn, meta = item
            if meta:
                results[fn] = meta
        elif isinstance(item, Exception):
            continue
    
    return results