"""
Discover GitHub org teams whose slug or name encodes a QA tier (1–3), list members,
and build login → max tier. Used by sync_qa_capabilities Phase 1.

Tier is parsed from team slug/name only (convention-based, not per-person maps).
If no such teams exist, callers fall back to activity-only inference.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

_log = logging.getLogger(__name__)

# Slug/name must suggest an explicit QA tier marker (avoid accidental matches).
_TIER_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # qa-tier-2, tier-3-qa, qa_tier_1
    re.compile(r"(?:^|[-_/])(?:qa[-_])?tier[-_]?([123])(?:$|[-_/])", re.IGNORECASE),
    # Display names: "QA Tier 3 Reviewers"
    re.compile(r"\btier\s*([123])\b", re.IGNORECASE),
    # t2-qa, t3_qa at start or after separator
    re.compile(r"(?:^|[-_/])t([123])[-_]qa(?:$|[-_/])", re.IGNORECASE),
    # qa-t2, qa-t3
    re.compile(r"(?:^|[-_/])qa[-_]t([123])(?:$|[-_/])", re.IGNORECASE),
    # level-3-qa, level_2_qa
    re.compile(r"(?:^|[-_/])level[-_]?([123])(?:$|[-_/]qa)", re.IGNORECASE),
)


def parse_qa_tier_from_github_team(slug: str, display_name: Optional[str] = None) -> Optional[int]:
    """
    Return 1, 2, or 3 if slug or GitHub team name encodes a QA tier; else None.

    Parsing rules are fixed here (team naming convention), not per-login maps.
    """
    for text in (slug or "", display_name or ""):
        t = (text or "").strip()
        if not t:
            continue
        for pat in _TIER_PATTERNS:
            m = pat.search(t)
            if m:
                return int(m.group(1))
    return None


async def _list_team_members(
    client: httpx.AsyncClient,
    org: str,
    team_slug: str,
    headers: dict[str, str],
) -> List[str]:
    """Lowercased GitHub logins for members of org/team_slug."""
    org_q, slug_q = quote(org, safe=""), quote(team_slug, safe="")
    path_base = f"https://api.github.com/orgs/{org_q}/teams/{slug_q}/members"
    out: List[str] = []
    page = 1
    while page <= 20:
        r = await client.get(f"{path_base}?per_page=100&page={page}", headers=headers)
        if r.status_code != 200:
            _log.debug("Team members %s/%s page %s: HTTP %s", org, team_slug, page, r.status_code)
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for u in batch:
            if isinstance(u, dict):
                login = (u.get("login") or "").strip().lower()
                if login:
                    out.append(login)
        if len(batch) < 100:
            break
        page += 1
    return out


async def fetch_login_max_qa_tier_from_org_teams(
    client: httpx.AsyncClient,
    org: str,
    headers: dict[str, str],
    *,
    skip_team_slug: Optional[str] = None,
) -> Tuple[Dict[str, int], List[str]]:
    """
    List all org teams; for each whose slug/name parses to tier 1–3, merge members.

    Returns:
      - mapping login(lower) -> max tier (3 wins over 1)
      - list of team slugs that contributed (for logging)
    """
    login_tier: Dict[str, int] = {}
    used_slugs: List[str] = []
    skip_slug = (skip_team_slug or "").strip().lower()

    page = 1
    while page <= 20:
        org_q = quote(org, safe="")
        url = f"https://api.github.com/orgs/{org_q}/teams?per_page=100&page={page}"
        r = await client.get(url, headers=headers)
        if r.status_code == 404:
            _log.warning("GitHub org teams not found for %r (404)", org)
            break
        if r.status_code == 403:
            _log.warning(
                "GitHub 403 listing teams for %r — token needs read:org (or admin:org). Tier teams skipped.",
                org,
            )
            break
        if r.status_code != 200:
            _log.warning("GitHub HTTP %s listing teams for %s: %s", r.status_code, org, r.text[:200])
            break

        teams = r.json()
        if not isinstance(teams, list) or not teams:
            break

        for team in teams:
            if not isinstance(team, dict):
                continue
            slug = (team.get("slug") or "").strip()
            name = (team.get("name") or "").strip() or None
            if not slug:
                continue
            if skip_slug and slug.lower() == skip_slug:
                continue
            tier = parse_qa_tier_from_github_team(slug, name)
            if tier is None:
                continue

            used_slugs.append(f"{slug}(t{tier})")
            logins = await _list_team_members(client, org, slug, headers)
            for login in logins:
                prev = login_tier.get(login, 0)
                login_tier[login] = max(prev, tier)

        if len(teams) < 100:
            break
        page += 1

    if used_slugs:
        _log.info(
            "QA tier teams in %s: %d team(s) with tier in name → %d member login(s). Examples: %s",
            org,
            len(used_slugs),
            len(login_tier),
            ", ".join(used_slugs[:12]) + ("…" if len(used_slugs) > 12 else ""),
        )
    else:
        _log.info(
            "No org teams in %s matched QA tier slug/name patterns — using activity-only qa_tier inference.",
            org,
        )

    return login_tier, used_slugs
