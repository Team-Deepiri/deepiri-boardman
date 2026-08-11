"""List repositories for a GitHub org (REST API, paginated)."""

from __future__ import annotations

import httpx

from boardman.settings import settings


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

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    async def _fetch_all(start_url: str) -> list[str]:
        url: str | None = start_url
        out: list[str] = []
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

    return sorted(set(names))
