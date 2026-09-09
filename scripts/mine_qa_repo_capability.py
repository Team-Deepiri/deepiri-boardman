"""
mine_qa_repo_capability.py

Own, no-vendor alternative to GitHunt/OSSInsight-style third-party "developer scores":
mine each QA's ACTUAL commit history (via PyDriller) across the repos they've really
contributed to, and bucket "what tier of work has this person demonstrably shipped."

Inspired by the metrics categories in chrkaatz/git-intelligence and
hoangsonww/GitIntel-MCP-Server (churn, contributor stats, complexity proxies via commit
history) -- both real, MIT-licensed, but Node/TypeScript LOCAL-repo analyzers with no
hosted API for an arbitrary GitHub login. This is the Python-native equivalent, using
PyDriller (a real "Mining Software Repositories" library on PyPI) directly instead of
requiring a second language runtime on the deploy box.

For each GitHub support-team member:
  1. Discover distinct repos they've authored/reviewed PRs in (GitHub Search API,
     same query shape as qa_activity_inference.py's activity signal), capped per
     person to bound total runtime.
  2. Classify each repo's difficulty tier with the SAME IDF-based tier_classifier used
     everywhere else in this codebase (repo_signals.json must exist -- run
     sync_qa_capabilities.py's Phase 0 first if it doesn't).
  3. Clone each repo to a throwaway temp dir and mine the person's own commits with
     PyDriller (churn, commit count, files touched) -- a failed/huge/unreachable repo
     is skipped, never aborts the whole run.
  4. Bucket the person's demonstrated tier from real, substantive activity (see
     boardman/github/repo_capability_mining.py's thresholds).

Written to the qa_capability_profiles DB table (NOT a JSON file -- a mining run writes
fresh rows every time, and that kind of churn has no business living in the git
working tree) -- read into an in-memory cache by
boardman.services.qa_capability_store.refresh_capability_cache(), which
boardman/assignment/config.py's live qa_tier resolution reads synchronously. This is
genuinely heavy (full clones + commit-history walks): run it periodically
(cron/manual), same cadence as sync_qa_capabilities.py's repo_signals.json /
worker_team.json.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.database.session import async_session
from boardman.github.repo_capability_mining import (
    demonstrated_tier_from_repo_stats,
    mined_author_stats_for_clone_url,
)
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.github.team_roster import fetch_support_team_members
from boardman.services.qa_capability_store import upsert_profile
from boardman.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("mine_qa_repo_capability")

TOKEN = settings.github_pat
ORG = settings.github_org
# Bounds total runtime: a prolific contributor could otherwise touch hundreds of repos.
MAX_REPOS_PER_PERSON = 10
MAX_SEARCH_PAGES = 2


async def _discover_repos(
    client: httpx.AsyncClient, login: str, org: str, headers: dict[str, str]
) -> list[str]:
    """Distinct `owner/repo` this login has authored or reviewed a PR in (org-scoped),
    newest activity first -- same query shape as qa_activity_inference.py."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for q in (f"is:pr org:{org} author:{login}", f"is:pr org:{org} reviewed-by:{login}"):
        for page in range(1, MAX_SEARCH_PAGES + 1):
            url = (
                "https://api.github.com/search/issues?q="
                f"{quote(q, safe='')}&sort=updated&order=desc&per_page=100&page={page}"
            )
            try:
                r = await client.get(url, headers=headers)
            except (httpx.HTTPError, OSError) as e:
                _log.debug("repo discovery %s: %s", q, e)
                break
            if r.status_code != 200:
                break
            items = (r.json() or {}).get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                repo_url = (item or {}).get("repository_url") or ""
                parts = str(repo_url).rstrip("/").split("/")
                fn = "/".join(parts[-2:]) if len(parts) >= 2 else ""
                if fn and "/" in fn and fn not in seen_set:
                    seen_set.add(fn)
                    seen.append(fn)
                    if len(seen) >= MAX_REPOS_PER_PERSON:
                        return seen
            if len(items) < 100:
                break
    return seen


async def _resolve_author_identities(
    client: httpx.AsyncClient, login: str, headers: dict[str, str]
) -> tuple[set[str], set[str]]:
    """Best-effort git-commit identities for a GitHub login: their public email (if
    set), GitHub's noreply address patterns (used automatically when a user keeps
    their email private -- the common case), and their display name."""
    emails = {f"{login.lower()}@users.noreply.github.com"}
    names: set[str] = set()
    try:
        r = await client.get(
            f"https://api.github.com/users/{quote(login, safe='')}", headers=headers
        )
        if r.status_code == 200:
            data = r.json() or {}
            uid = data.get("id")
            if uid:
                emails.add(f"{uid}+{login.lower()}@users.noreply.github.com")
            email = (data.get("email") or "").strip().lower()
            if email:
                emails.add(email)
            name = (data.get("name") or "").strip().lower()
            if name:
                names.add(name)
    except (httpx.HTTPError, OSError) as e:
        _log.debug("identity resolve for %s failed: %s", login, e)
    return emails, names


async def mine_one(
    client: httpx.AsyncClient, login: str, headers: dict[str, str]
) -> dict[str, Any]:
    repos = await _discover_repos(client, login, ORG, headers)
    if not repos:
        return {"qa_tier": None, "repos_mined": 0, "reason": "no PR activity found"}

    emails, names = await _resolve_author_identities(client, login, headers)

    tier_and_stats: list[tuple[int, Any]] = []
    repos_mined = 0
    for fn in repos:
        owner, repo = fn.split("/", 1)
        meta = await fetch_repo_metadata(client, owner, repo)
        if meta is None:
            continue
        tier, _ = classify_repo_tier(meta)
        clone_url = f"https://github.com/{fn}.git"
        # Mining is synchronous/blocking (real clone + disk I/O) -- run it off the
        # event loop so one slow repo does not stall every other async task in the batch.
        stats = await asyncio.to_thread(
            mined_author_stats_for_clone_url, clone_url, author_emails=emails, author_names=names
        )
        if stats is None:
            continue
        tier_and_stats.append((tier, stats))
        repos_mined += 1

    qa_tier = demonstrated_tier_from_repo_stats(tier_and_stats) if tier_and_stats else None
    return {
        "qa_tier": qa_tier,
        "repos_discovered": len(repos),
        "repos_mined": repos_mined,
    }


async def run() -> None:
    if not TOKEN:
        _log.error("GITHUB_PAT is not set -- cannot mine anything")
        return

    roster = await fetch_support_team_members()
    if not roster.get("ok"):
        _log.error("Failed to fetch roster: %s", roster.get("message"))
        return
    members = roster.get("members", [])
    _log.info(
        "%d team members found; mining up to %d repos each", len(members), MAX_REPOS_PER_PERSON
    )

    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    profiles: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for m in members:
            login = (m.get("login") or "").strip().lower()
            if not login:
                continue
            _log.info("Mining %s...", login)
            try:
                profiles[login] = await mine_one(client, login, headers)
            except Exception as e:  # noqa: BLE001 - one bad person must not sink the run
                _log.warning("mining %s failed: %s", login, e)
                profiles[login] = {
                    "qa_tier": None,
                    "repos_discovered": 0,
                    "repos_mined": 0,
                    "reason": str(e)[:200],
                }

    async with async_session() as session:
        for login, p in profiles.items():
            await upsert_profile(
                session,
                github_login=login,
                qa_tier=p.get("qa_tier"),
                repos_discovered=int(p.get("repos_discovered") or 0),
                repos_mined=int(p.get("repos_mined") or 0),
                reason=p.get("reason"),
            )
        await session.commit()
    _log.info("Wrote %d profile(s) to qa_capability_profiles", len(profiles))


if __name__ == "__main__":
    asyncio.run(run())
