"""Background worker: claim jobs from SQLite `background_jobs` and run handlers.

Run: ``python -m boardman.sqlite_worker`` (see docker-compose `boardman-worker`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from boardman.broker.job_queue import claim_next_job_row, fail_stale_running_jobs, mark_job_finished
from boardman.database.session import async_session, init_db
from boardman.jobs.handlers import JOB_HANDLERS
from boardman.logging_config import setup_logging
from boardman.observability.counters import background_work
from boardman.observability.degradation import log_degraded
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def _repo_knowledge_loop() -> None:
    """Keep cached repo knowledge honest without crawling anything.

    A reconciliation net for what the webhooks missed. Each cycle costs one cheap
    metadata call per repo; only a repo whose `pushed_at` actually moved is refetched, so
    a quiet ten minutes costs almost nothing.
    """
    from boardman.services.repo_knowledge import sweep_repo_knowledge, sweep_targets

    interval = max(60.0, float(settings.repo_knowledge_sweep_interval_seconds or 600.0))
    while True:
        await asyncio.sleep(interval)
        if not (settings.github_pat or "").strip():
            continue
        try:
            async with background_work():
                await sweep_repo_knowledge(
                    sweep_targets(),
                    concurrency=max(1, int(settings.repo_knowledge_sweep_concurrency or 3)),
                )
        except Exception:  # noqa: BLE001 - GitHub API failure degrades gracefully
            # A sweep is an optimisation. It must never take the worker down with it.
            log_degraded(_log, "repo knowledge sweep")


async def _reconciliation_loop() -> None:
    """Optional bounded safety net for webhook outages, owned by the existing worker."""
    from boardman.repos_config import list_registered_repos
    from boardman.services.reconcile import reconcile_repo

    while True:
        await asyncio.sleep(max(30.0, float(settings.github_reconcile_interval_seconds)))
        if not (settings.github_pat or "").strip():
            continue
        owner = (settings.github_bare_repo_owner or settings.github_org or "").strip()
        repos = []
        for key in list_registered_repos():
            full_name = key if "/" in key else f"{owner}/{key}"
            if full_name not in repos:
                repos.append(full_name)
        for full_name in repos:
            async with async_session() as session:
                try:
                    async with background_work():
                        result = await reconcile_repo(
                            full_name,
                            session,
                            max_items=max(1, min(int(settings.github_reconcile_max_items), 100)),
                        )
                        await session.commit()
                    _log.info(
                        "reconciliation repo=%s ok=%s issues=%s prs=%s errors=%s",
                        full_name,
                        result.get("ok"),
                        result.get("issues_checked"),
                        result.get("prs_checked"),
                        len(result.get("errors") or []),
                    )
                except Exception:  # noqa: BLE001 - graceful degradation
                    await session.rollback()
                    log_degraded(_log, f"reconciliation for {full_name}")


async def _run_one(job_id: str, kind: str, payload: dict) -> None:
    handler = JOB_HANDLERS.get(kind)
    if handler is None:
        await mark_job_finished(
            job_id,
            success=False,
            status="incomplete",
            result={"error": f"unknown job kind: {kind}"},
        )
        return
    try:
        async with background_work():
            out = await handler(payload)
        ok = bool(isinstance(out, dict) and out.get("ok", True))
        await mark_job_finished(
            job_id,
            success=ok,
            status="complete" if ok else "incomplete",
            result=out,
        )
    except Exception as e:  # noqa: BLE001 - observability failure must not affect the request
        _log.exception("job %s (%s) failed", job_id, kind)
        await mark_job_finished(
            job_id,
            success=False,
            status="incomplete",
            result={"error": str(e)},
        )


async def run_worker_forever() -> None:
    setup_logging()
    await init_db()
    n = await fail_stale_running_jobs(settings.queue_worker_stale_running_seconds)
    if n:
        _log.warning("Marked %d stale running jobs as incomplete", n)
    poll = settings.queue_worker_poll_seconds
    # Module/file name is historical (this queue was SQLite-only originally); the
    # log line names the ACTUAL backend so a 3am reader debugging a job issue on a
    # Postgres deployment isn't sent looking for a SQLite file that isn't in use.
    backend = "SQLite" if settings.database_url.startswith("sqlite") else "Postgres"
    _log.info(
        "%s-backed job worker started (poll=%ss, stale_running=%ss)",
        backend,
        poll,
        settings.queue_worker_stale_running_seconds,
    )
    reconcile_task = None
    if settings.github_reconcile_enabled:
        reconcile_task = asyncio.create_task(_reconciliation_loop(), name="github-reconciliation")
    knowledge_task = None
    if settings.repo_knowledge_sweep_enabled:
        knowledge_task = asyncio.create_task(_repo_knowledge_loop(), name="repo-knowledge-sweep")
        _log.info(
            "repo knowledge sweep every %.0fs (metadata-gated; only changed repos refetch)",
            settings.repo_knowledge_sweep_interval_seconds,
        )
    try:
        while True:
            row = await claim_next_job_row()
            if row is None:
                await asyncio.sleep(poll)
                continue
            job_id, kind, payload = row
            await _run_one(job_id, kind, payload)
    finally:
        for task in (reconcile_task, knowledge_task):
            if task is not None:
                task.cancel()


def main() -> None:
    try:
        asyncio.run(run_worker_forever())
    except KeyboardInterrupt:
        _log.info("worker stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
