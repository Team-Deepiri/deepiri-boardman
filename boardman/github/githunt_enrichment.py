"""GitHunt (https://githunt.ai) developer-scoring API: an OPTIONAL one-time cold-start
signal for a QA's repo-access tier when nobody has explicitly set one and no live
GitHub team name has either (see boardman/assignment/config.py's qa_tier resolution).

Free tier is 50 calls/month, so a login is looked up AT MOST ONCE ever and cached to
disk permanently (never TTL-expired, unlike every other live signal in this codebase)
-- a brand-new QA's cold-start score only matters once, before boardman's own decayed
PR-activity history (qa_activity_inference.py) takes over as the authoritative signal.

Empty GITHUNT_API_KEY = feature off entirely, same as every other optional live-data
source here: no network call, callers fall back to qa_tier_cold_start_default.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from boardman.settings import settings

_log = logging.getLogger(__name__)


def _cache_path() -> Path:
    p = Path(settings.githunt_cache_json_path)
    return p if p.is_absolute() else Path.cwd() / p


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError as exc:
        _log.warning("githunt cache write failed (%s): %s", path, exc)


def cached_profile(login: str) -> dict[str, Any] | None:
    """Cached GitHunt profile for this login, or None if never looked up.

    Distinct from "looked up and GitHunt had nothing" (also cached as None under the
    key) -- callers that only want to avoid a network call use `has_cached(login)`.
    """
    login_key = (login or "").strip().lower()
    if not login_key:
        return None
    return _load_cache().get(login_key)


def has_cached(login: str) -> bool:
    login_key = (login or "").strip().lower()
    return bool(login_key) and login_key in _load_cache()


def fetch_and_cache_profile_sync(login: str) -> dict[str, Any] | None:
    """Look up `login` on GitHunt and cache the result permanently (never re-queried).

    Returns None on ANY failure (no key configured, network error, 404, rate limit) --
    every caller must treat that the same as "no cold-start signal available" and fall
    back to qa_tier_cold_start_default. A miss is cached too (as None), otherwise a
    login GitHunt doesn't index burns a fresh call every single roster load forever --
    fatal against a 50-call/month quota.
    """
    login_key = (login or "").strip().lower()
    if not login_key:
        return None
    api_key = (settings.githunt_api_key or "").strip()
    if not api_key:
        return None

    cache = _load_cache()
    if login_key in cache:
        return cache[login_key] or None

    base = (settings.githunt_api_base or "https://api.githunt.ai").rstrip("/")
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{base}/v1/users/{login_key}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code != 200:
            _log.info(
                "githunt lookup for %s: HTTP %s (caching as no signal)", login_key, r.status_code
            )
            cache[login_key] = None
            _save_cache(cache)
            return None
        data = r.json()
        profile = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(profile, dict):
            cache[login_key] = None
            _save_cache(cache)
            return None
        cache[login_key] = profile
        _save_cache(cache)
        return profile
    except Exception as exc:  # noqa: BLE001 - an enrichment failure must not block roster load
        _log.warning("githunt lookup failed for %s: %s", login_key, exc)
        return None


def qa_tier_from_profile(profile: dict[str, Any] | None) -> int | None:
    """Bucket a GitHunt profile's activity/tech-stack score (0-100 each) into a
    starting qa_tier (1-3). None when the profile has neither score.

    Advisory only -- this seeds where a brand-new QA starts; boardman's own decayed
    PR-activity history is what actually governs their tier from then on, so the exact
    thresholds here matter far less than "don't start everyone at the same default."
    """
    if not isinstance(profile, dict):
        return None
    activity = profile.get("activity_score")
    tech = profile.get("tech_stack_score")
    vals = [float(v) for v in (activity, tech) if isinstance(v, int | float)]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    if avg >= 70:
        return 3
    if avg >= 35:
        return 2
    return 1
