"""
Infer QA tier (1–3) from GitHub PR search (author + reviewed-by), decayed by time,
weighted by inferred repo tier. No LLM; no per-login tables.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from boardman.assignment.tier_classifier import classify_repo_tier
from boardman.github.repo_metadata import fetch_repo_metadata

_log = logging.getLogger(__name__)


def _parse_github_datetime(value: str) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _decay_weight(updated_at: str, *, now: datetime, half_life_days: float) -> float:
    """1.0 at t=0; halves every half_life_days."""
    if half_life_days <= 0:
        return 1.0
    dt = _parse_github_datetime(updated_at)
    if dt is None:
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return math.pow(0.5, days / half_life_days)


async def _repo_tier(
    client: httpx.AsyncClient,
    repo_fn: str,
    repo_cache: dict[str, int],
) -> int:
    if repo_fn in repo_cache:
        return repo_cache[repo_fn]
    if "/" not in repo_fn:
        repo_cache[repo_fn] = 2
        return 2
    owner, repo = repo_fn.split("/", 1)
    meta = await fetch_repo_metadata(client, owner, repo)
    tier, _ = classify_repo_tier(meta)
    repo_cache[repo_fn] = tier
    return tier


async def infer_qa_tier_from_pr_activity(
    client: httpx.AsyncClient,
    login: str,
    search_org: str,
    headers: dict[str, str],
    repo_cache: dict[str, int],
    *,
    half_life_days: float = 180.0,
    max_search_pages: int = 5,
    tier3_min_distinct_t3_repos: int = 2,
    tier3_min_weighted_score: float = 5.0,
    tier2_min_distinct_t2plus_repos: int = 3,
    tier2_min_weighted_score: float = 2.5,
) -> Tuple[int, Dict[str, Any]]:
    """
    Paginate GitHub search for PRs authored by and reviewed by login.

    Returns (qa_tier, debug dict for logging/metrics).
    """
    now = datetime.now(timezone.utc)
    seen_pr: set[str] = set()
    repo_max_tier: dict[str, int] = {}
    repo_decay_sum: dict[str, float] = defaultdict(float)

    queries = (
        f"is:pr org:{search_org} author:{login}",
        f"is:pr org:{search_org} reviewed-by:{login}",
    )

    for q in queries:
        for page in range(1, max(1, max_search_pages) + 1):
            url = (
                "https://api.github.com/search/issues?q="
                f"{quote(q, safe='')}&per_page=100&page={page}"
            )
            try:
                r = await client.get(url, headers=headers)
            except Exception as e:
                _log.debug("search issues %s: %s", q, e)
                break
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                repo_url = item.get("repository_url") or ""
                if not isinstance(repo_url, str):
                    continue
                parts = repo_url.rstrip("/").split("/")
                repo_fn = "/".join(parts[-2:]) if len(parts) >= 2 else ""
                if "/" not in repo_fn:
                    continue
                num = item.get("number")
                if num is None:
                    continue
                pr_key = f"{repo_fn}#{num}"
                if pr_key in seen_pr:
                    continue
                seen_pr.add(pr_key)

                updated = (item.get("updated_at") or item.get("closed_at") or "") or ""
                w = _decay_weight(str(updated), now=now, half_life_days=half_life_days)
                rt = await _repo_tier(client, repo_fn, repo_cache)
                prev_t = repo_max_tier.get(repo_fn, 0)
                repo_max_tier[repo_fn] = max(prev_t, rt)
                repo_decay_sum[repo_fn] += w * float(rt)

    distinct_t3 = sum(1 for t in repo_max_tier.values() if t == 3)
    distinct_t2p = sum(1 for t in repo_max_tier.values() if t >= 2)
    weighted_score = sum(repo_decay_sum.values())

    tier = 1
    if distinct_t3 >= tier3_min_distinct_t3_repos and weighted_score >= tier3_min_weighted_score:
        tier = 3
    elif distinct_t2p >= tier2_min_distinct_t2plus_repos or weighted_score >= tier2_min_weighted_score:
        tier = 2

    debug: Dict[str, Any] = {
        "distinct_t3_repos": distinct_t3,
        "distinct_t2plus_repos": distinct_t2p,
        "weighted_score": round(weighted_score, 3),
        "pr_sample_count": len(seen_pr),
        "repos_touched": len(repo_max_tier),
    }
    return tier, debug
