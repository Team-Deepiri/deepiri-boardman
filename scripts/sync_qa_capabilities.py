"""
sync_qa_capabilities.py

Two-phase sync:

Phase 0 — Build IDF model (repo_signals.json)
  - Fetch file tree of every org repo
  - Every file basename and directory name becomes a signal
  - IDF(signal) = log(N / repos_containing_signal)
  - Score(repo) = sum of IDF scores for its signals
  - Percentile thresholds (p50, p80) become tier boundaries
  - Written to repo_signals.json — read at runtime by tier_classifier

Phase 1 — QA capability sync (worker_team.json)
  - Fetch GitHub support team roster
  - For each member, look at their recent PR/review history
  - Classify the repos they touched using Phase 0 tiers
  - qa_tier = highest tier they have meaningful history on
  - Match GitHub login → Plaky ID via identity_match
  - Written to worker_team.json — loaded into Cloudflare Worker

Run periodically (e.g. nightly via cron) to keep both files fresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.github.org_repos import fetch_org_repository_full_names
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.github.team_roster import fetch_support_team_members
from boardman.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("sync")

TOKEN = settings.github_pat
ORG = settings.github_org
SIGNALS_PATH = settings.repo_signals_json_path
_RESOLVED_ORG: str = ""   # set in build_idf_model after auto-discovery
TEAM_PATH = "worker_team.json"
YAML_PATH = settings.team_assignments_yml_path

# How many PR/review interactions on a tier-N repo to consider the member capable
PROMOTION_THRESHOLDS = {3: 2, 2: 3}


# ═══════════════════════════════════════════════════════════════════
# Phase 0 — IDF model
# ═══════════════════════════════════════════════════════════════════

async def build_idf_model(client: httpx.AsyncClient) -> None:
    """
    Scan all org repos, collect file-tree signals, compute IDF, write repo_signals.json.
    No hardcoded meanings — rarity across the org determines weight.
    """
    _log.info("Phase 0: fetching org repo list for %s...", ORG)

    # Try configured org, fall back to auto-discovering from PAT's org memberships
    repo_names = []
    try:
        repo_names = await fetch_org_repository_full_names(client, ORG)
    except Exception:
        pass

    global _RESOLVED_ORG
    if not repo_names:
        headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
        r = await client.get("https://api.github.com/user/orgs", headers=headers)
        for org in ([o["login"] for o in r.json()] if r.status_code == 200 else []):
            try:
                repo_names = await fetch_org_repository_full_names(client, org)
                if repo_names:
                    _RESOLVED_ORG = org
                    break
            except Exception:
                continue
    else:
        _RESOLVED_ORG = ORG

    N = len(repo_names)
    if N == 0:
        _log.error("No repos found — cannot build IDF model")
        return

    _log.info("Fetching file trees for %d repos (batches of 8)...", N)

    # Collect raw_signals per repo
    repo_signals: dict[str, list[str]] = {}
    BATCH = 8
    for i in range(0, N, BATCH):
        batch = repo_names[i : i + BATCH]
        tasks = []
        for fn in batch:
            owner, repo = fn.split("/", 1)
            tasks.append(fetch_repo_metadata(client, owner, repo))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for fn, result in zip(batch, results):
            if isinstance(result, Exception) or result is None:
                repo_signals[fn] = []
            else:
                repo_signals[fn] = result.raw_signals
        _log.info("  %d/%d repos scanned", min(i + BATCH, N), N)

    # Document frequency: how many repos contain each signal
    df: dict[str, int] = {}
    for signals in repo_signals.values():
        for sig in set(signals):   # set: each repo counts once per signal
            df[sig] = df.get(sig, 0) + 1

    # IDF: log(N / df)
    idf: dict[str, float] = {
        sig: math.log(N / count)
        for sig, count in df.items()
        if count > 0
    }

    # Score every repo
    repo_scores: dict[str, float] = {
        fn: sum(idf.get(sig, 0.0) for sig in signals)
        for fn, signals in repo_signals.items()
    }

# Fully dynamic: percentiles from score distribution
    sorted_scores = sorted(repo_scores.values())
    n = len(sorted_scores)
    
    if n < 4:
        p50 = sorted_scores[n // 2]
        p80 = sorted_scores[-1]
    else:
        q1 = sorted_scores[n // 4]
        q3 = sorted_scores[3 * n // 4]
        
        p50 = q1
        p80 = q3

    output: dict[str, Any] = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_repos": N,
        "idf": idf,
        "percentiles": {"p50": p50, "p80": p80},
        "repo_scores": repo_scores,
    }

    with open(SIGNALS_PATH, "w") as f:
        json.dump(output, f, indent=2)

    _log.info(
        "Phase 0 done: %d signals, p50=%.2f, p80=%.2f → %s",
        len(idf), p50, p80, SIGNALS_PATH,
    )

    # Log tier distribution
    t1 = sum(1 for s in scores_sorted if s < p50)
    t2 = sum(1 for s in scores_sorted if p50 <= s < p80)
    t3 = sum(1 for s in scores_sorted if s >= p80)
    _log.info("Tier distribution: T1=%d  T2=%d  T3=%d", t1, t2, t3)


# ═══════════════════════════════════════════════════════════════════
# Phase 1 — QA capability sync
# ═══════════════════════════════════════════════════════════════════

def load_yaml_overrides() -> tuple[dict, dict]:
    if not os.path.exists(YAML_PATH):
        return {}, {}
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f) or {}
    return data.get("member_overrides", {}), data.get("member_defaults", {})


async def infer_member_qa_tier(
    client: httpx.AsyncClient,
    login: str,
    headers: dict,
    repo_cache: dict[str, int],
) -> int:
    """
    Infer qa_tier from a member's recent PR/review history across the org.
    Checks repos they authored or reviewed, classifies those repos, promotes tier
    based on thresholds. Zero hardcoded tier assignments.
    """
    tier_counts = {1: 0, 2: 0, 3: 0}
    # Use discovered org name for search (may differ from settings.github_org)
    search_org = _RESOLVED_ORG or ORG
    queries = [
        f"is:pr org:{search_org} author:{login}",
        f"is:pr org:{search_org} reviewed-by:{login}",
    ]
    for q in queries:
        url = f"https://api.github.com/search/issues?q={q}&per_page=20"
        try:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                continue
            for item in r.json().get("items", []):
                repo_url = item.get("repository_url", "")
                # repo_url = https://api.github.com/repos/Owner/Repo
                repo_fn = "/".join(repo_url.split("/")[-2:])
                if repo_fn not in repo_cache:
                    if "/" in repo_fn:
                        owner, repo = repo_fn.split("/", 1)
                        meta = await fetch_repo_metadata(client, owner, repo)
                        tier, _ = classify_repo_tier(meta)
                        repo_cache[repo_fn] = tier
                    else:
                        repo_cache[repo_fn] = 2
                tier_counts[repo_cache[repo_fn]] += 1
        except Exception:
            continue

    # Infer highest tier with sufficient history
    if tier_counts[3] >= PROMOTION_THRESHOLDS[3]:
        return 3
    if tier_counts[2] >= PROMOTION_THRESHOLDS[2]:
        return 2
    return 1


async def sync_qa_team(client: httpx.AsyncClient) -> None:
    """
    Phase 1: Build worker_team.json from GitHub roster + inferred qa_tiers.
    """
    _log.info("Phase 1: fetching GitHub support team roster...")
    roster = await fetch_support_team_members()
    if not roster.get("ok"):
        _log.error("Failed to fetch roster: %s", roster.get("message"))
        return

    members = roster.get("members", [])
    overrides, defaults = load_yaml_overrides()
    _log.info("%d team members found", len(members))

    gh_headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    # Fetch Plaky users for identity matching
    from boardman.plaky.client import PlakyClient
    from boardman.assignment.identity_match import best_plaky_match_for_github

    plaky_users: list[dict] = []
    pr = PlakyClient().list_workspace_users_sync()
    if pr.get("ok"):
        plaky_users = [u for u in (pr.get("users") or []) if isinstance(u, dict)]

    repo_cache: dict[str, int] = {}
    worker_members = []

    for m in members:
        login = (m.get("login") or "").lower()
        ov = overrides.get(login, {})

        # Resolve Plaky ID: override > auto-match
        plaky_id = str(ov.get("id") or ov.get("plaky_id") or "").strip()
        if not plaky_id and plaky_users:
            matched, _, _ = best_plaky_match_for_github(m, plaky_users)
            if matched:
                plaky_id = matched

        if not plaky_id:
            _log.warning("No Plaky ID for %s — skipping", login)
            continue

        # Infer qa_tier from actual git history
        _log.info("Inferring qa_tier for %s...", login)
        if "qa_tier" in ov:
            qa_tier = int(ov["qa_tier"])
        else:
            qa_tier = await infer_member_qa_tier(client, login, gh_headers, repo_cache)

        worker_members.append({
            "id": plaky_id,
            "display": ov.get("display") or m.get("name") or login,
            "github_login": login,
            "roles": ov.get("roles") or defaults.get("roles") or ["engineer", "qa"],
            "qaTier": qa_tier,
            "repoGlobs": ov.get("repo_globs") or defaults.get("repo_globs") or [f"{ORG}/*", "Team-Deepiri/*"],
            "weight": float(ov.get("weight") or defaults.get("weight") or 1.0),
            "tier": ov.get("tier") or defaults.get("tier") or "standard",
        })
        _log.info("  %s → qa_tier=%d  plaky_id=%s", login, qa_tier, plaky_id)

    with open(TEAM_PATH, "w") as f:
        json.dump(worker_members, f, indent=2)

    _log.info("Phase 1 done: %d members written to %s", len(worker_members), TEAM_PATH)


# ═══════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    if not TOKEN:
        _log.error("GITHUB_PAT not set in .env")
        return

    async with httpx.AsyncClient(timeout=30) as client:
        await build_idf_model(client)
        await sync_qa_team(client)


if __name__ == "__main__":
    asyncio.run(main())
