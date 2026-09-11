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
     person to bound total runtime. Org-scoped (Team-Deepiri) first; if that turns up
     fewer than GLOBAL_FALLBACK_THRESHOLD repos, ALSO searches all of GitHub and
     merges in what it finds -- "no PRs in Deepiri yet" is not the same claim as
     "no evidence anywhere," and someone new to this org may have a real,
     demonstrated track record elsewhere that deserves to count.
  2. Classify each repo's difficulty tier with the SAME IDF-based tier_classifier used
     everywhere else in this codebase (repo_signals.json must exist -- run
     sync_qa_capabilities.py's Phase 0 first if it doesn't). Memoized in a
     run-wide cache: many QAs share the same handful of repos, so a repo's tier is
     only ever fetched/classified once per run, not once per person who touched it.
  3. Clone each repo to a throwaway temp dir and mine the person's own commits with
     PyDriller (churn, commit count, files touched) -- a failed/huge/unreachable repo
     is skipped, never aborts the whole run. Clones run concurrently, bounded by
     CLONE_CONCURRENCY across the WHOLE run (not per person), so the total wall-clock
     cost is roughly (total repo-mines / CLONE_CONCURRENCY) rather than one-at-a-time.
  4. Weight-average the person's demonstrated tier from real, substantive activity
     into a FRACTIONAL qa_tier (e.g. 1.7, not forced to round to 1/2/3 -- see
     boardman/github/repo_capability_mining.py's demonstrated_tier_from_repo_stats).

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
# Full clones run concurrently up to this many at once (across the whole run, not per
# person) -- bounded so a big batch doesn't spike disk I/O/CPU or the ephemeral
# container's memory limit all at once. Repo-metadata fetches (cheap API calls) are
# not bounded by this; only the actual clone+mine work is.
CLONE_CONCURRENCY = 4
# Below this many org-scoped repos discovered, a person is "thin evidence in Deepiri"
# and worth searching GLOBALLY too (see mine_one) -- someone new to this org may still
# have a real, demonstrated track record elsewhere on GitHub, and "no Deepiri PRs yet"
# is not the same claim as "no evidence anywhere."
GLOBAL_FALLBACK_THRESHOLD = 2


async def _discover_repos(
    client: httpx.AsyncClient, login: str, headers: dict[str, str], *, org: str | None
) -> list[str]:
    """Distinct `owner/repo` this login has authored or reviewed a PR in, newest
    activity first -- same query shape as qa_activity_inference.py. `org=None` searches
    ALL of GitHub instead of one org (see GLOBAL_FALLBACK_THRESHOLD)."""
    scope = f"org:{org} " if org else ""
    seen: list[str] = []
    seen_set: set[str] = set()
    for q in (f"is:pr {scope}author:{login}", f"is:pr {scope}reviewed-by:{login}"):
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


async def _repo_tier(
    client: httpx.AsyncClient, fn: str, repo_tier_cache: dict[str, int]
) -> int | None:
    """Tier for `fn` (owner/repo), memoized across the whole run -- many QAs share the
    same handful of repos, so without this cache every one of them re-fetched and
    re-classified the same repo's metadata from scratch."""
    if fn in repo_tier_cache:
        return repo_tier_cache[fn]
    owner, repo = fn.split("/", 1)
    meta = await fetch_repo_metadata(client, owner, repo)
    if meta is None:
        return None
    tier, _ = classify_repo_tier(meta)
    repo_tier_cache[fn] = tier
    return tier


async def _clone_and_mine_one_repo(
    fn: str,
    tier: int,
    *,
    emails: set[str],
    names: set[str],
    clone_semaphore: asyncio.Semaphore,
) -> tuple[int, Any] | None:
    # Authenticated clone URL -- most Team-Deepiri repos are private, and an
    # unauthenticated https clone fails with git exit 128 (as seen against prod: 8 of
    # the org's repos rejected the plain URL). The PAT works as either username or
    # password over HTTPS; this is the same TOKEN already proven valid for the REST
    # calls used to discover/classify the repo.
    clone_url = f"https://{TOKEN}@github.com/{fn}.git"
    async with clone_semaphore:
        # Mining is synchronous/blocking (real clone + disk I/O) -- run it off the
        # event loop so it doesn't block other concurrent clones or API calls; the
        # semaphore, not the event loop, is what bounds how many run at once.
        stats = await asyncio.to_thread(
            mined_author_stats_for_clone_url,
            clone_url,
            author_emails=emails,
            author_names=names,
            redact_secret=TOKEN,
        )
    if stats is None:
        return None
    return tier, stats


async def mine_one(
    client: httpx.AsyncClient,
    login: str,
    headers: dict[str, str],
    *,
    repo_tier_cache: dict[str, int],
    clone_semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    repos = await _discover_repos(client, login, headers, org=ORG)
    used_global = False
    if len(repos) < GLOBAL_FALLBACK_THRESHOLD:
        # Thin (or zero) evidence inside Deepiri is not the same claim as "no evidence
        # anywhere" -- someone new to this org may have a real track record elsewhere
        # on GitHub. Search globally too and merge, still capped at
        # MAX_REPOS_PER_PERSON overall.
        global_repos = await _discover_repos(client, login, headers, org=None)
        seen = set(repos)
        for fn in global_repos:
            if fn not in seen:
                repos.append(fn)
                seen.add(fn)
                used_global = True
                if len(repos) >= MAX_REPOS_PER_PERSON:
                    break
    if not repos:
        return {"qa_tier": None, "repos_mined": 0, "reason": "no PR activity found anywhere"}

    emails, names = await _resolve_author_identities(client, login, headers)

    # Tier lookups are cheap API calls (cached besides) -- fetch them all concurrently
    # rather than one at a time.
    tiers = await asyncio.gather(*(_repo_tier(client, fn, repo_tier_cache) for fn in repos))
    clone_tasks = [
        _clone_and_mine_one_repo(
            fn, tier, emails=emails, names=names, clone_semaphore=clone_semaphore
        )
        for fn, tier in zip(repos, tiers, strict=True)
        if tier is not None
    ]
    # The semaphore inside each task is what actually bounds concurrent clones to
    # CLONE_CONCURRENCY; gathering all of them here just lets that bound run at full
    # capacity instead of leaving it idle between one finishing and the next starting.
    results = await asyncio.gather(*clone_tasks)
    tier_and_stats = [r for r in results if r is not None]

    qa_tier = demonstrated_tier_from_repo_stats(tier_and_stats) if tier_and_stats else None
    return {
        "qa_tier": qa_tier,
        "repos_discovered": len(repos),
        "repos_mined": len(tier_and_stats),
        "reason": "includes evidence from outside Team-Deepiri" if used_global else None,
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
    repo_tier_cache: dict[str, int] = {}
    clone_semaphore = asyncio.Semaphore(CLONE_CONCURRENCY)
    async with httpx.AsyncClient(timeout=30) as client:
        for m in members:
            login = (m.get("login") or "").strip().lower()
            if not login:
                continue
            _log.info("Mining %s...", login)
            try:
                profiles[login] = await mine_one(
                    client,
                    login,
                    headers,
                    repo_tier_cache=repo_tier_cache,
                    clone_semaphore=clone_semaphore,
                )
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
