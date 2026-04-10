"""GitHub org team roster (e.g. Team-Deepiri/support-team) for support / assignment identity."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from boardman.github.repo_fetch import github_request, github_request_sync
from boardman.settings import settings

_log = logging.getLogger(__name__)

# (monotonic_ts, team_spec, result dict) — invalidated by clear_support_team_cache()
_support_roster_cache: Optional[Tuple[float, str, Dict[str, Any]]] = None
SUPPORT_ROSTER_TTL_SEC = 120.0


def clear_support_team_cache() -> None:
    global _support_roster_cache
    _support_roster_cache = None


def get_cached_support_team_roster(team_spec: str) -> Dict[str, Any]:
    """TTL cache for assignment loader (sync). Cleared by reload_team_assignments()."""
    import time

    global _support_roster_cache
    spec = team_spec.strip()
    now = time.monotonic()
    if (
        _support_roster_cache is not None
        and _support_roster_cache[1] == spec
        and now - _support_roster_cache[0] < SUPPORT_ROSTER_TTL_SEC
    ):
        return _support_roster_cache[2]
    result = fetch_support_team_members_sync(team_spec=spec)
    _support_roster_cache = (now, spec, result)
    return result


def parse_github_team_spec(spec: str) -> Optional[Tuple[str, str]]:
    """
    `org/team-slug` as in @Team-Deepiri/support-team → ("Team-Deepiri", "support-team").
    Only the first `/` splits org from team slug (slug may contain hyphens, not slashes).
    """
    s = (spec or "").strip()
    if "/" not in s:
        return None
    org, slug = s.split("/", 1)
    org, slug = org.strip(), slug.strip()
    if not org or not slug:
        return None
    return org, slug


async def _enrich_public_names(client: httpx.AsyncClient, members: List[Dict[str, Any]]) -> None:
    """GET /users/{login} for display name (optional; team list often has login only)."""
    sem = asyncio.Semaphore(8)

    async def one(row: Dict[str, Any]) -> None:
        login = row.get("login")
        if not login:
            return
        async with sem:
            r = await github_request(client, f"/users/{quote(str(login), safe='')}")
        if r.status_code != 200:
            return
        data = r.json()
        if isinstance(data, dict):
            nm = (data.get("name") or "").strip()
            if nm:
                row["name"] = nm
            em = (data.get("email") or "").strip()
            if em:
                row["email"] = em.lower()

    await asyncio.gather(*[one(m) for m in members])


async def fetch_support_team_members(
    *,
    team_spec: Optional[str] = None,
    enrich_names: bool = True,
) -> Dict[str, Any]:
    """
    List members of the configured GitHub org team (needs PAT with read:org or org scope).

    Returns: ok, message, team (spec string), members[{login, name?, id, avatar_url, html_url}]
    """
    spec = (team_spec if team_spec is not None else settings.github_support_team).strip()
    parsed = parse_github_team_spec(spec)
    if not parsed:
        return {
            "ok": False,
            "message": "Invalid team spec: use org/team-slug (e.g. Team-Deepiri/support-team).",
            "team": spec,
            "members": [],
        }

    if not (settings.github_pat or "").strip():
        return {
            "ok": False,
            "message": "GITHUB_PAT is not set — cannot list GitHub team members.",
            "team": spec,
            "members": [],
        }

    org, team_slug = parsed
    org_q, slug_q = quote(org, safe=""), quote(team_slug, safe="")
    path_base = f"/orgs/{org_q}/teams/{slug_q}/members"

    out: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        page = 1
        while page <= 20:
            r = await github_request(client, f"{path_base}?per_page=100&page={page}")
            if r.status_code == 404:
                return {
                    "ok": False,
                    "message": (
                        f"GitHub team not found: {spec!r}. "
                        "Check org login and team slug; PAT needs read:org (or org membership read)."
                    ),
                    "team": spec,
                    "members": [],
                }
            if r.status_code == 403:
                return {
                    "ok": False,
                    "message": (
                        f"GitHub returned 403 for {spec!r}. "
                        "Ensure the token has read:org and can see this team."
                    ),
                    "team": spec,
                    "members": [],
                }
            if r.status_code != 200:
                return {
                    "ok": False,
                    "message": f"GitHub API error {r.status_code}: {r.text[:300]}",
                    "team": spec,
                    "members": [],
                }
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            for u in batch:
                if not isinstance(u, dict):
                    continue
                login = (u.get("login") or "").strip()
                if not login:
                    continue
                out.append(
                    {
                        "login": login,
                        "id": u.get("id"),
                        "avatar_url": u.get("avatar_url"),
                        "html_url": u.get("html_url"),
                    }
                )
            if len(batch) < 100:
                break
            page += 1

        if enrich_names and out:
            try:
                await _enrich_public_names(client, out)
            except Exception as e:
                _log.warning("Could not enrich GitHub names: %s", e)

    out.sort(key=lambda x: (x.get("name") or x.get("login") or "").lower())
    return {"ok": True, "message": "", "team": spec, "members": out}


def _enrich_public_names_sync(client: httpx.Client, members: List[Dict[str, Any]]) -> None:
    for row in members:
        login = row.get("login")
        if not login:
            continue
        r = github_request_sync(client, f"/users/{quote(str(login), safe='')}")
        if r.status_code != 200:
            continue
        data = r.json()
        if isinstance(data, dict):
            nm = (data.get("name") or "").strip()
            if nm:
                row["name"] = nm
            em = (data.get("email") or "").strip()
            if em:
                row["email"] = em.lower()


def fetch_support_team_members_sync(
    *,
    team_spec: Optional[str] = None,
    enrich_names: bool = True,
) -> Dict[str, Any]:
    """Blocking fetch for load_team_assignments() (same semantics as async version)."""
    spec = (team_spec if team_spec is not None else settings.github_support_team).strip()
    parsed = parse_github_team_spec(spec)
    if not parsed:
        return {
            "ok": False,
            "message": "Invalid team spec: use org/team-slug (e.g. Team-Deepiri/support-team).",
            "team": spec,
            "members": [],
        }

    if not (settings.github_pat or "").strip():
        return {
            "ok": False,
            "message": "GITHUB_PAT is not set — cannot list GitHub team members.",
            "team": spec,
            "members": [],
        }

    org, team_slug = parsed
    org_q, slug_q = quote(org, safe=""), quote(team_slug, safe="")
    path_base = f"/orgs/{org_q}/teams/{slug_q}/members"

    out: List[Dict[str, Any]] = []
    with httpx.Client(timeout=30) as client:
        page = 1
        while page <= 20:
            r = github_request_sync(client, f"{path_base}?per_page=100&page={page}")
            if r.status_code == 404:
                return {
                    "ok": False,
                    "message": (
                        f"GitHub team not found: {spec!r}. "
                        "Check org login and team slug; PAT needs read:org (or org membership read)."
                    ),
                    "team": spec,
                    "members": [],
                }
            if r.status_code == 403:
                return {
                    "ok": False,
                    "message": (
                        f"GitHub returned 403 for {spec!r}. "
                        "Ensure the token has read:org and can see this team."
                    ),
                    "team": spec,
                    "members": [],
                }
            if r.status_code != 200:
                return {
                    "ok": False,
                    "message": f"GitHub API error {r.status_code}: {r.text[:300]}",
                    "team": spec,
                    "members": [],
                }
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            for u in batch:
                if not isinstance(u, dict):
                    continue
                login = (u.get("login") or "").strip()
                if not login:
                    continue
                out.append(
                    {
                        "login": login,
                        "id": u.get("id"),
                        "avatar_url": u.get("avatar_url"),
                        "html_url": u.get("html_url"),
                    }
                )
            if len(batch) < 100:
                break
            page += 1

        if enrich_names and out:
            try:
                _enrich_public_names_sync(client, out)
            except Exception as e:
                _log.warning("Could not enrich GitHub names (sync): %s", e)

    out.sort(key=lambda x: (x.get("name") or x.get("login") or "").lower())
    return {"ok": True, "message": "", "team": spec, "members": out}
