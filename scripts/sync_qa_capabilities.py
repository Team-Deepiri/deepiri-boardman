"""
sync_qa_capabilities.py

Two-phase sync:

Phase 0 — Build IDF model (repo_signals.json)
  - Fetch file tree of every org repo
  - Every file basename and directory name becomes a signal
  - IDF(signal) = log(N / repos_containing_signal)
  - Score(repo) = IDF sum + 0.2 * structural (matches tier_classifier)
  - Percentile thresholds (p50/p80 keys; ~25th/~70th index) become tier boundaries
  - Written to repo_signals.json — read at runtime by tier_classifier

Phase 1 — QA capability sync (worker_team.json)
  - Fetch GitHub support team roster
  - For each member, look at their recent PR/review history
  - Classify the repos they touched using Phase 0 tiers
  - qa_tier: org teams with tier in slug/name (qa-tier-3, …) override; else decayed PR activity vs repo tiers
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

from boardman.assignment.tier_classifier import classify_repo_tier, compute_structural_complexity_score
from boardman.github.org_repos import fetch_org_repository_full_names
from boardman.github.qa_activity_inference import infer_qa_tier_from_pr_activity
from boardman.github.qa_tier_teams import fetch_login_max_qa_tier_from_org_teams
from boardman.github.repo_metadata import fetch_repo_metadata
from boardman.github.team_roster import fetch_support_team_members, parse_github_team_spec
from boardman.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger("sync")

TOKEN = settings.github_pat
ORG = settings.github_org
SIGNALS_PATH = settings.repo_signals_json_path
_RESOLVED_ORG: str = ""   # set in build_idf_model after auto-discovery
TEAM_PATH = "worker_team.json"
YAML_PATH = settings.team_assignments_yml_path

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

    # Collect raw_signals + metadata per repo (structural score must match tier_classifier)
    repo_signals: dict[str, list[str]] = {}
    repo_metas: dict[str, Any] = {}
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
                repo_metas[fn] = result
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

    # Score every repo (same blend as boardman.assignment.tier_classifier.classify_repo_tier)
    repo_scores: dict[str, float] = {}
    for fn, signals in repo_signals.items():
        idf_part = sum(idf.get(sig, 0.0) for sig in signals)
        meta = repo_metas.get(fn)
        struct = compute_structural_complexity_score(meta) if meta is not None else 0.0
        repo_scores[fn] = idf_part + 0.2 * struct

# Use percentiles: T1=bottom ~25%, T2=middle, T3=top rest
    sorted_scores = sorted(repo_scores.values())
    n = len(sorted_scores)
    
    if n < 6:
        p50 = sorted_scores[n // 2]
        p80 = sorted_scores[-1]
    else:
        p50 = sorted_scores[int(n * 0.25)]
        p80 = sorted_scores[int(n * 0.7)]

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
    t1 = sum(1 for s in sorted_scores if s < p50)
    t2 = sum(1 for s in sorted_scores if p50 <= s < p80)
    t3 = sum(1 for s in sorted_scores if s >= p80)
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

    search_org = (_RESOLVED_ORG or ORG or "").strip()
    parsed_team = parse_github_team_spec(settings.github_support_team)
    org_for_tier_teams = (parsed_team[0] if parsed_team else search_org or ORG).strip()
    support_slug_lower = (parsed_team[1] if parsed_team else "").strip().lower()

    team_login_tier: dict[str, int] = {}
    if settings.github_qa_tier_team_scan_enabled and org_for_tier_teams:
        try:
            team_login_tier, _tier_team_slugs = await fetch_login_max_qa_tier_from_org_teams(
                client,
                org_for_tier_teams,
                gh_headers,
                skip_team_slug=support_slug_lower or None,
            )
        except Exception as e:
            _log.warning("QA tier team scan failed (%s); using activity-only.", e)

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

        _log.info("Inferring qa_tier for %s...", login)
        if "qa_tier" in ov:
            qa_tier = int(ov["qa_tier"])
            activity_debug: dict[str, Any] = {}
        else:
            activity_tier, activity_debug = await infer_qa_tier_from_pr_activity(
                client,
                login,
                search_org,
                gh_headers,
                repo_cache,
                half_life_days=settings.github_qa_activity_half_life_days,
                max_search_pages=settings.github_qa_activity_search_max_pages,
                tier3_min_distinct_t3_repos=settings.github_qa_activity_tier3_min_distinct_t3_repos,
                tier3_min_weighted_score=settings.github_qa_activity_tier3_min_weighted_score,
                tier2_min_distinct_t2plus_repos=settings.github_qa_activity_tier2_min_distinct_t2plus_repos,
                tier2_min_weighted_score=settings.github_qa_activity_tier2_min_weighted_score,
            )
            team_tier = team_login_tier.get(login) if settings.github_qa_tier_team_scan_enabled else None
            if team_tier is not None:
                qa_tier = team_tier
            else:
                qa_tier = activity_tier

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
        if "qa_tier" not in ov:
            _log.info(
                "  %s → qa_tier=%d plaky_id=%s (activity_debug=%s team_hit=%s)",
                login,
                qa_tier,
                plaky_id,
                activity_debug,
                team_login_tier.get(login) if settings.github_qa_tier_team_scan_enabled else None,
            )
        else:
            _log.info("  %s → qa_tier=%d plaky_id=%s (override)", login, qa_tier, plaky_id)

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
