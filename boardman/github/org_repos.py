"""List repositories for a GitHub org (REST API, paginated)."""

from __future__ import annotations

import time

import httpx

from boardman.settings import settings

# Org repo listing is a hot path: the agent calls it to resolve repo names on almost every
# turn, and it costs a full paginated crawl (~1.5s for 57 repos). Repos appear rarely, so a
# short in-process TTL cache removes that latency without going stale in any practical way.
_ORG_REPOS_TTL_SECONDS = 600.0
_org_repos_cache: dict[tuple[str, bool], tuple[float, list[str]]] = {}
# The same crawl already downloads open_issues_count and pushed_at for every repo; it used
# to discard them and then "which repos are busiest" had no way to answer. Same request,
# same TTL, kept rather than thrown away.
_org_rows_cache: dict[tuple[str, bool], tuple[float, list[dict]]] = {}
# The key is (org, skip_archived), naturally tiny for a single-org deployment. Capped
# anyway so a future multi-org deployment cannot grow either cache without bound.
_ORG_CACHE_MAX_ENTRIES = 32


def _evict_oldest_if_over_cap(cache: dict[tuple[str, bool], tuple[float, list]]) -> None:
    if len(cache) <= _ORG_CACHE_MAX_ENTRIES:
        return
    oldest_key = min(cache, key=lambda k: cache[k][0])
    cache.pop(oldest_key, None)


def clear_org_repos_cache() -> None:
    _org_repos_cache.clear()
    _org_rows_cache.clear()


def cached_org_repo_rows(org: str, *, skip_archived: bool = True) -> list[dict]:
    """Activity rows from the last crawl, or [] if none is cached. Never fetches."""
    hit = _org_rows_cache.get((org.strip().casefold(), bool(skip_archived)))
    return list(hit[1]) if hit and hit[0] > time.monotonic() else []


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


async def fetch_org_repository_full_names(
    client: httpx.AsyncClient,
    org: str,
    *,
    skip_archived: bool = True,
) -> list[str]:
    token = settings.github_pat
    if not token:
        return []

    cache_key = (org.strip().casefold(), bool(skip_archived))
    hit = _org_repos_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return list(hit[1])

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    rows: list[dict] = []

    async def _fetch_all(start_url: str) -> list[str]:
        url: str | None = start_url
        out: list[str] = []
        rows.clear()
        while url:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            for repo in r.json():
                if skip_archived and repo.get("archived"):
                    continue
                fn = repo.get("full_name")
                if fn:
                    out.append(str(fn))
                    rows.append(
                        {
                            "full_name": str(fn),
                            # GitHub's open_issues_count counts PULL REQUESTS TOO. Named
                            # exactly as GitHub means it so no caller can mistake it for
                            # an issue-only figure.
                            "open_issues_and_prs": int(repo.get("open_issues_count") or 0),
                            "pushed_at": str(repo.get("pushed_at") or ""),
                            "language": str(repo.get("language") or ""),
                            "private": bool(repo.get("private")),
                        }
                    )
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
            orgs_resp = await client.get(
                "https://api.github.com/user/orgs?per_page=100", headers=headers
            )
            orgs_resp.raise_for_status()
            discovered = [
                str(o.get("login", "")).strip() for o in (orgs_resp.json() or []) if o.get("login")
            ]
            names = []
            for candidate in discovered:
                candidate_url = (
                    f"https://api.github.com/orgs/{candidate}/repos?per_page=100&type=all"
                )
                try:
                    names = await _fetch_all(candidate_url)
                except httpx.HTTPStatusError:
                    continue
                if names:
                    break

    result = sorted(set(names))
    if result:
        expiry = time.monotonic() + _ORG_REPOS_TTL_SECONDS
        _org_repos_cache[cache_key] = (expiry, list(result))
        _org_rows_cache[cache_key] = (expiry, list(rows))
        _evict_oldest_if_over_cap(_org_repos_cache)
        _evict_oldest_if_over_cap(_org_rows_cache)
    return result
