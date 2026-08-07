"""List repositories for a GitHub org (REST API, paginated)."""

from __future__ import annotations

import time
from typing import List, Optional

import httpx

from boardman.settings import settings

# Org repo listing is a hot path: the agent calls it to resolve repo names on almost every
# turn, and it costs a full paginated crawl (~1.5s for 57 repos). Repos appear rarely, so a
# short in-process TTL cache removes that latency without going stale in any practical way.
_ORG_REPOS_TTL_SECONDS = 600.0
_org_repos_cache: dict[tuple[str, bool], tuple[float, List[str]]] = {}


def clear_org_repos_cache() -> None:
    _org_repos_cache.clear()


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


async def fetch_org_repository_full_names(
    client: httpx.AsyncClient,
    org: str,
    *,
    skip_archived: bool = True,
) -> List[str]:
    token = settings.github_pat
    if not token:
        return []

    cache_key = (org.strip().casefold(), bool(skip_archived))
    hit = _org_repos_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return list(hit[1])

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    async def _fetch_all(start_url: str) -> List[str]:
        url: Optional[str] = start_url
        out: List[str] = []
        while url:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            for repo in r.json():
                if skip_archived and repo.get("archived"):
                    continue
                fn = repo.get("full_name")
                if fn:
                    out.append(str(fn))
            url = _parse_next_url(r.headers.get("Link"))
        return out

    org_url = f"https://api.github.com/orgs/{org}/repos?per_page=100&type=all"
    try:
        names = await _fetch_all(org_url)
    except httpx.HTTPStatusError as exc:
        # Some installations configure a GitHub owner that is a user account, not an org.
        # In that case /orgs/{owner}/repos returns 404 while /users/{owner}/repos works.
        if exc.response.status_code != 404:
            raise
        user_url = f"https://api.github.com/users/{org}/repos?per_page=100&type=all"
        try:
            names = await _fetch_all(user_url)
        except httpx.HTTPStatusError as user_exc:
            if user_exc.response.status_code != 404:
                raise
            # Final fallback: discover orgs visible to this PAT and try each.
            orgs_resp = await client.get("https://api.github.com/user/orgs?per_page=100", headers=headers)
            orgs_resp.raise_for_status()
            discovered = [str(o.get("login", "")).strip() for o in (orgs_resp.json() or []) if o.get("login")]
            names = []
            for candidate in discovered:
                candidate_url = f"https://api.github.com/orgs/{candidate}/repos?per_page=100&type=all"
                try:
                    names = await _fetch_all(candidate_url)
                except httpx.HTTPStatusError:
                    continue
                if names:
                    break

    result = sorted(set(names))
    if result:
        _org_repos_cache[cache_key] = (time.monotonic() + _ORG_REPOS_TTL_SECONDS, list(result))
    return result
