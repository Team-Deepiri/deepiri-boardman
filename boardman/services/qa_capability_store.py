"""DB-backed store for QaCapabilityProfile (own commit-history-mined QA cold-start
signal, see boardman/github/repo_capability_mining.py and
scripts/mine_qa_repo_capability.py).

Deliberately a database table, not a JSON file in the working tree: a mining run
writes here on every run, and a data file that churns on every run has no business
living in the git working directory (nothing here is ever committed). The live
qa_tier resolution in boardman/assignment/config.py is synchronous, though, so it
never queries this table directly -- it reads an in-memory cache
(`cached_capability_tiers()`) that `refresh_capability_cache()` populates from here.
Call `refresh_capability_cache()` at app/worker startup and periodically thereafter
(e.g. from the same loop that refreshes other TTL-cached roster data).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import QaCapabilityProfile
from boardman.database.session import async_session

_log = logging.getLogger(__name__)

# {github_login (lowercased) -> qa_tier}. Populated ONLY by refresh_capability_cache();
# read by the synchronous config.py roster loader via cached_capability_tiers().
# Fractional (e.g. 1.7) -- a weighted blend of demonstrated evidence, not forced to
# round to the nearest whole bucket.
_capability_cache: dict[str, float] = {}
_capability_cache_loaded_at: float = 0.0


async def upsert_profile(
    session: AsyncSession,
    *,
    github_login: str,
    qa_tier: float | None,
    repos_discovered: int = 0,
    repos_mined: int = 0,
    reason: str | None = None,
) -> None:
    login = (github_login or "").strip().lower()
    if not login:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    reason_trimmed = (reason or "")[:255] or None
    # Portable select-then-write (no dialect-specific upsert): this table is written
    # once per login per mining run, not a hot path, so the extra round trip is fine --
    # and it works identically on the SQLite dev DB and the Postgres prod DB.
    existing = (
        await session.execute(
            select(QaCapabilityProfile).where(QaCapabilityProfile.github_login == login)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.qa_tier = qa_tier
        existing.repos_discovered = repos_discovered
        existing.repos_mined = repos_mined
        existing.reason = reason_trimmed
        existing.computed_at = now
    else:
        session.add(
            QaCapabilityProfile(
                github_login=login,
                qa_tier=qa_tier,
                repos_discovered=repos_discovered,
                repos_mined=repos_mined,
                reason=reason_trimmed,
                computed_at=now,
            )
        )


async def all_profiles(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """{github_login -> {qa_tier, repos_discovered, repos_mined, reason, computed_at}}."""
    rows = (await session.execute(select(QaCapabilityProfile))).scalars().all()
    return {
        row.github_login: {
            "qa_tier": row.qa_tier,
            "repos_discovered": row.repos_discovered,
            "repos_mined": row.repos_mined,
            "reason": row.reason,
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
        }
        for row in rows
    }


async def refresh_capability_cache() -> None:
    """Reload the in-memory {login -> qa_tier} cache from the DB. Best-effort: a
    failure here must not take down the caller (app startup, a periodic refresh loop)
    -- the previous cache contents (possibly empty) are kept."""
    global _capability_cache, _capability_cache_loaded_at
    try:
        async with async_session() as session:
            profiles = await all_profiles(session)
    except Exception as exc:  # noqa: BLE001 - a failed refresh must not crash the caller
        _log.warning("qa_capability_store: refresh failed, keeping previous cache: %s", exc)
        return
    _capability_cache = {
        login: float(p["qa_tier"])
        for login, p in profiles.items()
        if isinstance(p.get("qa_tier"), int | float) and 1.0 <= p["qa_tier"] <= 3.0
    }
    _capability_cache_loaded_at = time.monotonic()


def cached_capability_tiers() -> dict[str, float]:
    """Synchronous read of the in-memory cache -- safe to call from config.py's
    synchronous roster loader. Empty until refresh_capability_cache() has run at least
    once (e.g. at app/worker startup); that's the same "no data yet" contract as every
    other optional cache in this module."""
    return dict(_capability_cache)


def clear_capability_cache() -> None:
    """Test/reload hook -- mirrors reload_team_assignments() clearing other caches."""
    global _capability_cache, _capability_cache_loaded_at
    _capability_cache = {}
    _capability_cache_loaded_at = 0.0
