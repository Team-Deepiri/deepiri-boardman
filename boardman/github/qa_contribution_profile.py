"""Per-login GitHub contribution profiles for QA-fit scoring.

Built from the same PR search the tier inference uses (authored + reviewed-by,
recency-decayed), enriched with each contributed repo's primary language and
topics. The QA picker turns these into cosine-similarity scores against the
target repo, so assignment favors QAs whose real GitHub history fits the task.

All lookups are cached in-process with TTLs — a webhook pick re-uses profiles
for hours instead of re-searching GitHub per event.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

from boardman.github.qa_activity_inference import _decay_weight
from boardman.settings import settings

_log = logging.getLogger(__name__)

PROFILE_TTL_SECONDS = 6 * 3600.0
REPO_INFO_TTL_SECONDS = 24 * 3600.0
# Keep per-login search cheap: profiles need shape, not completeness.
PROFILE_MAX_SEARCH_PAGES = 2
PROFILE_MAX_DISTINCT_REPOS = 30

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class RepoInfo:
    full_name: str
    language: str = ""
    topics: List[str] = field(default_factory=list)
    description: str = ""

    def tokens(self) -> Dict[str, float]:
        """Word bag from the repo name, topics, and description."""
        bag: Dict[str, float] = defaultdict(float)
        name = self.full_name.split("/", 1)[-1]
        for t in _TOKEN_RE.findall(name.lower()):
            bag[t] += 2.0  # name tokens are the strongest identity signal
        for topic in self.topics:
            for t in _TOKEN_RE.findall(str(topic).lower()):
                bag[t] += 1.5
        for t in _TOKEN_RE.findall((self.description or "").lower())[:40]:
            bag[t] += 0.5
        return dict(bag)


@dataclass
class QaContributionProfile:
    login: str
    # owner/repo -> recency-decayed contribution weight (authored + reviewed PRs)
    repo_weights: Dict[str, float] = field(default_factory=dict)
    # language -> aggregated weight across contributed repos
    language_weights: Dict[str, float] = field(default_factory=dict)
    # token -> aggregated weight from contributed repo names/topics/descriptions
    token_weights: Dict[str, float] = field(default_factory=dict)
    pr_sample_count: int = 0

    def top_repos(self, n: int = 3) -> List[str]:
        return [fn for fn, _ in sorted(self.repo_weights.items(), key=lambda kv: -kv[1])[:n]]


_repo_info_cache: dict[str, tuple[float, RepoInfo]] = {}
_profile_cache: dict[str, tuple[float, QaContributionProfile]] = {}
_disk_cache_loaded = False

# Expiries use wall-clock time.time() so the disk cache survives process restarts.
CACHE_FILENAME = ".qa_profiles_cache.json"


def _cache_path() -> str:
    import os

    base = os.path.dirname(os.path.abspath(settings.repo_signals_json_path)) or "."
    return os.path.join(base, CACHE_FILENAME)


def _maybe_load_disk_cache() -> None:
    """Populate in-memory caches from disk once per process (expired entries dropped)."""
    global _disk_cache_loaded
    if _disk_cache_loaded:
        return
    _disk_cache_loaded = True
    import json
    import os

    path = _cache_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        for key, row in (data.get("profiles") or {}).items():
            if float(row.get("expires", 0)) > now:
                p = QaContributionProfile(
                    login=row.get("login", ""),
                    repo_weights=row.get("repo_weights") or {},
                    language_weights=row.get("language_weights") or {},
                    token_weights=row.get("token_weights") or {},
                    pr_sample_count=int(row.get("pr_sample_count") or 0),
                )
                _profile_cache[key] = (float(row["expires"]), p)
        for fn, row in (data.get("repo_info") or {}).items():
            if float(row.get("expires", 0)) > now:
                info = RepoInfo(
                    full_name=fn,
                    language=row.get("language", ""),
                    topics=row.get("topics") or [],
                    description=row.get("description", ""),
                )
                _repo_info_cache[fn] = (float(row["expires"]), info)
        _log.info(
            "qa profiles: loaded disk cache (%d profiles, %d repo infos)",
            len(_profile_cache),
            len(_repo_info_cache),
        )
    except Exception as e:  # noqa: BLE001 — cache corruption must never break picking
        _log.warning("qa profiles: could not load disk cache: %s", e)


def _save_disk_cache() -> None:
    import json
    import os

    path = _cache_path()
    try:
        data = {
            "profiles": {
                key: {
                    "expires": exp,
                    "login": p.login,
                    "repo_weights": p.repo_weights,
                    "language_weights": p.language_weights,
                    "token_weights": p.token_weights,
                    "pr_sample_count": p.pr_sample_count,
                }
                for key, (exp, p) in _profile_cache.items()
            },
            "repo_info": {
                fn: {
                    "expires": exp,
                    "language": i.language,
                    "topics": i.topics,
                    "description": i.description,
                }
                for fn, (exp, i) in _repo_info_cache.items()
            },
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        _log.debug("qa profiles: could not save disk cache: %s", e)


def clear_contribution_caches() -> None:
    _repo_info_cache.clear()
    _profile_cache.clear()


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }


async def fetch_repo_info(client: httpx.AsyncClient, full_name: str) -> Optional[RepoInfo]:
    """GET /repos/{full_name} → language + topics + description (24h cache, disk-backed)."""
    _maybe_load_disk_cache()
    now = time.time()
    hit = _repo_info_cache.get(full_name)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = await client.get(f"https://api.github.com/repos/{full_name}", headers=_gh_headers())
    except Exception as e:
        _log.debug("repo info %s: %s", full_name, e)
        return hit[1] if hit else None
    if r.status_code != 200:
        return hit[1] if hit else None
    data = r.json()
    if not isinstance(data, dict):
        return None
    info = RepoInfo(
        full_name=full_name,
        language=str(data.get("language") or ""),
        topics=[str(t) for t in (data.get("topics") or []) if t],
        description=str(data.get("description") or ""),
    )
    _repo_info_cache[full_name] = (now + REPO_INFO_TTL_SECONDS, info)
    return info


async def fetch_contribution_profile(
    client: httpx.AsyncClient,
    login: str,
    search_org: str,
    *,
    half_life_days: Optional[float] = None,
) -> Optional[QaContributionProfile]:
    """Search PRs authored/reviewed by ``login`` in ``search_org``; aggregate into a profile.

    Returns None when GitHub is unreachable or unauthorized (callers fall back to
    config-weight picking). Cached 6h per login.
    """
    if not (settings.github_pat or "").strip():
        return None
    _maybe_load_disk_cache()
    key = f"{search_org}:{login}".lower()
    now = time.time()
    hit = _profile_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    hl = half_life_days if half_life_days is not None else settings.github_qa_activity_half_life_days
    now_dt = datetime.now(timezone.utc)
    repo_weights: Dict[str, float] = defaultdict(float)
    seen_pr: set[str] = set()
    any_response = False

    # Reviews weigh more than authorship: this profile ranks QA (review) fitness.
    queries = (
        (f"is:pr org:{search_org} author:{login}", 1.0),
        (f"is:pr org:{search_org} reviewed-by:{login}", 1.25),
    )
    for q, qw in queries:
        for page in range(1, PROFILE_MAX_SEARCH_PAGES + 1):
            url = f"https://api.github.com/search/issues?q={quote(q, safe='')}&per_page=100&page={page}"
            try:
                r = await client.get(url, headers=_gh_headers())
                if r.status_code in (403, 429):
                    # Search rate limit — honor Retry-After (capped) once, then retry.
                    wait = min(float(r.headers.get("Retry-After") or 8), 15.0)
                    _log.debug("profile search %s throttled; retrying in %.0fs", q, wait)
                    await asyncio.sleep(wait)
                    r = await client.get(url, headers=_gh_headers())
            except Exception as e:
                _log.debug("profile search %s: %s", q, e)
                break
            if r.status_code != 200:
                # Still throttled or auth problem — keep whatever we have.
                _log.debug("profile search %s -> HTTP %s", q, r.status_code)
                break
            any_response = True
            data = r.json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                repo_url = str(item.get("repository_url") or "")
                parts = repo_url.rstrip("/").split("/")
                repo_fn = "/".join(parts[-2:]) if len(parts) >= 2 else ""
                num = item.get("number")
                if "/" not in repo_fn or num is None:
                    continue
                # Dedup within ONE query (its result pages) — the full query string is the
                # namespace. A truncated q[:12] collided across the author/reviewed-by
                # queries and silently dropped the reviewed-by weighting.
                pr_key = f"{q}:{repo_fn}#{num}"
                if pr_key in seen_pr:
                    continue
                seen_pr.add(pr_key)
                updated = str(item.get("updated_at") or item.get("closed_at") or "")
                repo_weights[repo_fn] += qw * _decay_weight(updated, now=now_dt, half_life_days=hl)

    if not any_response:
        return hit[1] if hit else None

    top = dict(sorted(repo_weights.items(), key=lambda kv: -kv[1])[:PROFILE_MAX_DISTINCT_REPOS])
    profile = QaContributionProfile(login=login, repo_weights=top, pr_sample_count=len(seen_pr))

    infos = await asyncio.gather(*(fetch_repo_info(client, fn) for fn in top), return_exceptions=True)
    lang: Dict[str, float] = defaultdict(float)
    toks: Dict[str, float] = defaultdict(float)
    for fn, info in zip(top, infos):
        w = top[fn]
        if not isinstance(info, RepoInfo):
            continue
        if info.language:
            lang[info.language.lower()] += w
        for t, tw in info.tokens().items():
            toks[t] += w * tw
    profile.language_weights = dict(lang)
    profile.token_weights = dict(toks)

    _profile_cache[key] = (now + PROFILE_TTL_SECONDS, profile)
    _save_disk_cache()
    return profile


def cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity of two sparse weight vectors (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    dot = sum(w * b[k] for k, w in a.items() if k in b)
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def direct_contribution_score(profile: QaContributionProfile, target_full_name: str) -> float:
    """0..1 saturation of the member's decayed contribution weight on the target repo itself."""
    w = 0.0
    key = target_full_name.strip().lower()
    for fn, weight in profile.repo_weights.items():
        if fn.strip().lower() == key:
            w = weight
            break
    return 1.0 - math.exp(-w)


def _roster_search_org() -> str:
    """Org whose PRs we search when warming profiles: support-team org > bare owner > github_org."""
    team = (settings.github_support_team or "").strip()
    if "/" in team:
        return team.split("/", 1)[0]
    return (settings.github_bare_repo_owner or "").strip() or (settings.github_org or "").strip()


async def warm_qa_profiles_loop(*, member_delay_seconds: float = 5.0) -> None:
    """Background task: pre-fetch QA contribution profiles so live picks hit warm cache.

    One member every few seconds (GitHub search allows ~30 req/min and each profile
    costs 2+ search calls), then sleeps half the profile TTL and refreshes. Started
    from the app lifespan when qa_github_fit_enabled and a PAT are configured.
    """
    while True:
        try:
            from boardman.assignment.config import load_team_assignments

            cfg = await asyncio.to_thread(load_team_assignments)
            org = _roster_search_org()
            qa_logins = [
                m.github_login.strip()
                for m in cfg.members
                if "qa" in m.roles and (m.github_login or "").strip()
            ]
            _log.info("qa profiles: warming %d logins for org %s", len(qa_logins), org)
            warmed = 0
            async with httpx.AsyncClient(timeout=30.0) as client:
                for login in qa_logins:
                    p = await fetch_contribution_profile(client, login, org)
                    if p is not None:
                        warmed += 1
                    await asyncio.sleep(max(1.0, member_delay_seconds))
            _log.info("qa profiles: warmed %d/%d", warmed, len(qa_logins))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — warming must never crash the app
            _log.warning("qa profile warmer error: %s", e)
        await asyncio.sleep(PROFILE_TTL_SECONDS / 2)
