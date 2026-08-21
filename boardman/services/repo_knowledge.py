"""The periodic net under the webhooks.

Webhooks are the fast path for freshness and they carry the load; this is the sweep that
catches what they missed — a delivery that failed, an event GitHub never sent, a repo
nobody has webhooks on. It runs in the worker, never on a request.

The design constraint is that it must be nearly idle when nothing has changed. A sweep
that re-reads every repository every ten minutes is a crawler, not a reconciliation pass,
and it would spend the API budget it exists to protect. So each cycle costs exactly one
cheap metadata call per repo, comparing GitHub's ``pushed_at`` against the value stored on
the snapshot when it was built. Only a repo that actually moved pays for a refetch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import ProjectContext

logger = logging.getLogger(__name__)


async def stored_revision(session: AsyncSession, repo: str) -> str | None:
    """The ``pushed_at`` the stored snapshot was built from, or None if there is none."""
    try:
        row = (
            await session.execute(select(ProjectContext).where(ProjectContext.repo == repo))
        ).scalar_one_or_none()
    except SQLAlchemyError:
        await session.rollback()
        return None
    if row is None or not row.context_json:
        return None
    return str(row.context_source_revision or "") or None


async def current_revision(repo: str) -> tuple[str, str]:
    """(pushed_at, error) for a repo. One GitHub call, deliberately the cheapest one."""
    from boardman.github.http import shared_github_client
    from boardman.settings import settings

    token = (settings.github_pat or "").strip()
    if not token or "/" not in repo:
        return "", "no token or malformed repo"
    # Uses the shared client pool (connection reuse + counting hook). Headers are passed
    # explicitly because github_request is internal to repo_fetch.
    owner, name = repo.split("/", 1)
    try:

        async with shared_github_client() as client:
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{name}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=20.0,
            )
    except (httpx.HTTPError, OSError, ValueError) as e:
        return "", f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"
    try:
        return str((r.json() or {}).get("pushed_at") or ""), ""
    except ValueError:
        return "", "unparseable response"


async def refresh_if_moved(session: AsyncSession, repo: str) -> dict[str, Any]:
    """Refresh one repo's briefing only if GitHub says it changed.

    Returns what happened, so a sweep can report honestly rather than claiming work it
    skipped. A repo whose ``pushed_at`` is unchanged costs exactly one API call.
    """
    known = await stored_revision(session, repo)
    live, error = await current_revision(repo)
    if error:
        return {"repo": repo, "action": "error", "error": error}
    if known and live and known == live:
        return {"repo": repo, "action": "unchanged", "revision": live}

    from boardman.jobs.handlers import boardman_repo_refresh_job

    out = await boardman_repo_refresh_job({"repo": repo})
    return {
        "repo": repo,
        "action": "refreshed" if out.get("ok") else "failed",
        "was": known or "(none)",
        "now": live,
        "error": out.get("error", ""),
    }


async def sweep_repo_knowledge(
    repos: list[str],
    *,
    concurrency: int = 3,
    session_factory: Any = None,
) -> dict[str, Any]:
    """One reconciliation cycle over ``repos``. One broken repo never stops the rest."""
    if not repos:
        return {"ok": True, "checked": 0, "refreshed": 0, "results": []}
    if session_factory is None:
        from boardman.database.session import async_session as session_factory  # noqa: N813

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(repo: str) -> dict[str, Any]:
        async with sem:
            try:
                async with session_factory() as session:
                    result = await refresh_if_moved(session, repo)
                    await session.commit()
                    return result
            except (
                Exception
            ) as e:  # noqa: BLE001 - observability failure must not affect the request
                logger.warning("knowledge sweep failed for %s: %s", repo, e)
                return {"repo": repo, "action": "error", "error": str(e)[:200]}

    results = await asyncio.gather(*(one(r) for r in repos), return_exceptions=False)
    refreshed = sum(1 for r in results if r.get("action") == "refreshed")
    errors = [r for r in results if r.get("action") == "error"]
    logger.info(
        "knowledge sweep: %d checked, %d refreshed, %d unchanged, %d errors",
        len(results),
        refreshed,
        sum(1 for r in results if r.get("action") == "unchanged"),
        len(errors),
    )
    from boardman.observability.counters import bump

    bump("brain.sweeps")
    bump("brain.sweep_refreshes", refreshed)
    return {
        "ok": True,
        "checked": len(results),
        "refreshed": refreshed,
        "errors": len(errors),
        "results": results,
    }


def sweep_targets() -> list[str]:
    """The repos worth sweeping: everything routed, capped, most specific first."""
    from boardman.repos_config import list_registered_repos
    from boardman.settings import settings

    owner = (settings.github_bare_repo_owner or settings.github_org or "").strip()
    out: list[str] = []
    for key in list_registered_repos():
        full = key if "/" in key else (f"{owner}/{key}" if owner else key)
        if "/" in full and full not in out:
            out.append(full)
    return out[: max(1, int(settings.repo_knowledge_sweep_max_repos or 25))]
