"""List repositories for a GitHub org (REST API, paginated)."""

from __future__ import annotations

from typing import List, Optional

import httpx

from boardman.settings import settings


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

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    url: Optional[str] = f"https://api.github.com/orgs/{org}/repos?per_page=100&type=all"
    names: List[str] = []

    while url:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        for repo in r.json():
            if skip_archived and repo.get("archived"):
                continue
            fn = repo.get("full_name")
            if fn:
                names.append(str(fn))
        url = _parse_next_url(r.headers.get("Link"))

    return sorted(set(names))
