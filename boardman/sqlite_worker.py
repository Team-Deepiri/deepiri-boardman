"""Background worker: claim jobs from SQLite `background_jobs` and run handlers.

Run: ``python -m boardman.sqlite_worker`` (see docker-compose `boardman-worker`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from boardman.broker.job_queue import claim_next_job_row, fail_stale_running_jobs, mark_job_finished
from boardman.database.session import async_session, init_db
from boardman.github.auth import github_auth_available
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
        if not github_auth_available():
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
        if not github_auth_available():
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


async def _pr_task_lifecycle_loop() -> None:
    """Sweep tasks the PR pipeline created or linked: delete orphaned "created" tasks
    past their TTL, archive completed "matched" tasks off their working board."""
    from boardman.services.pr_task_lifecycle import (
        archive_completed_matched_tasks,
        cleanup_orphaned_pr_tasks,
    )

    interval = max(60.0, float(settings.pr_task_cleanup_interval_seconds or 3600.0))
    while True:
        await asyncio.sleep(interval)
        async with async_session() as session:
            try:
                async with background_work():
                    cleanup_res = await cleanup_orphaned_pr_tasks(session)
                    archive_res = await archive_completed_matched_tasks(session)
                _log.info(
                    "pr task lifecycle sweep: cleanup=%s archive=%s", cleanup_res, archive_res
                )
            except Exception:  # noqa: BLE001 - graceful degradation
                await session.rollback()
                log_degraded(_log, "pr task lifecycle sweep")


async def _qa_capability_cache_loop() -> None:
    """Keep boardman.assignment.config's in-memory qa_capability_profiles cache from
    going permanently stale in a long-running process. The underlying DB table only
    changes when scripts/mine_qa_repo_capability.py is actually run (rare -- it's a
    heavy full-clone mining pass), so this loop is cheap: just a DB read on an interval,
    never anything that clones or mines."""
    from boardman.services.qa_capability_store import refresh_capability_cache

    interval = max(300.0, float(settings.qa_capability_cache_refresh_interval_seconds or 3600.0))
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_capability_cache()
        except Exception:  # noqa: BLE001 - a stale cache is fine; a dead loop is not
            log_degraded(_log, "qa capability cache refresh")


def _nudge_sweep_counts(results: list[dict]) -> tuple[int, int]:
    """Split one sweep's results into (comments that went out, rows retired).

    `sweep_due_nudges` returns an entry for two very different outcomes: a comment that
    was actually posted, and a PR that had already merged or closed, so its escalation
    state was deleted instead of mentioned. Counting both as "sent" made a sweep that
    suppressed a nudge report the same number as one that @mentioned somebody -- which
    is precisely the line an operator reads while working out why a merged PR was (or
    wasn't) being mentioned.

    A row skipped for an unreadable PR state is not in `results` at all, and one skipped
    for having no recipient is counted as neither: nothing was sent and nothing was
    retired, so both numbers stay honest about what left this process.
    """
    sent = sum(1 for r in results if not r.get("skipped"))
    retired = sum(1 for r in results if r.get("retired"))
    return sent, retired


async def _pr_review_nudge_loop() -> None:
    """Stale-PR @mention escalation sweep (boardman/services/pr_review_nudges.py).

    The comment/review/push handlers keep each tracked PR's escalation CLOCK current as
    events arrive, so the common case is a local comparison against the small
    pr_review_nudges table and costs no GitHub call at all. A row that is actually due,
    though, is checked against live GitHub state before anyone is mentioned: a row says
    who owes a review, never whether there is still a review to owe. Without that read a
    PR that merged on day 2 kept being @mentioned at days 3, 6, 9, 12 and then daily,
    forever (deepiri-mudspeed#50).

    So the per-tick cost is zero calls for rows nobody is waiting on yet, and one read
    plus at most one comment for the rows that are due. A PR that turned out to be merged
    or closed is retired rather than mentioned, and one whose state could not be read is
    skipped without advancing its stage, so the next sweep retries it rather than losing
    the escalation.
    """
    from boardman.services.pr_review_nudges import sweep_due_nudges

    interval = max(300.0, float(settings.pr_review_nudge_sweep_interval_seconds or 3600.0))
    while True:
        await asyncio.sleep(interval)
        async with async_session() as session:
            try:
                async with background_work():
                    results = await sweep_due_nudges(session)
                if results:
                    sent, retired = _nudge_sweep_counts(results)
                    _log.info("pr review nudge sweep: sent %d, retired %d", sent, retired)
            except Exception:  # noqa: BLE001 - graceful degradation
                await session.rollback()
                log_degraded(_log, "pr review nudge sweep")


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
    lifecycle_task = None
    if settings.pr_task_cleanup_enabled:
        lifecycle_task = asyncio.create_task(
            _pr_task_lifecycle_loop(), name="pr-task-lifecycle-sweep"
        )
        _log.info(
            "pr task lifecycle sweep every %.0fs (ttl=%.0fd for orphaned created tasks)",
            settings.pr_task_cleanup_interval_seconds,
            settings.pr_task_cleanup_ttl_days,
        )
    capability_cache_task = asyncio.create_task(
        _qa_capability_cache_loop(), name="qa-capability-cache-refresh"
    )
    review_nudge_task = None
    if settings.pr_review_nudge_enabled:
        review_nudge_task = asyncio.create_task(
            _pr_review_nudge_loop(), name="pr-review-nudge-sweep"
        )
        _log.info(
            "pr review nudge sweep every %.0fs (due PRs are checked against live GitHub "
            "state before anyone is mentioned; merged/closed ones are retired)",
            settings.pr_review_nudge_sweep_interval_seconds,
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
        for task in (
            reconcile_task,
            knowledge_task,
            lifecycle_task,
            capability_cache_task,
            review_nudge_task,
        ):
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
