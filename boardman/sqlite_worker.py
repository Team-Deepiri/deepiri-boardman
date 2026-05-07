"""Background worker: claim jobs from SQLite `background_jobs` and run handlers.

Run: ``python -m boardman.sqlite_worker`` (see docker-compose `boardman-worker`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from boardman.broker.job_queue import claim_next_job_row, fail_stale_running_jobs, mark_job_finished
from boardman.database.session import init_db
from boardman.jobs.handlers import JOB_HANDLERS
from boardman.logging_config import setup_logging
from boardman.settings import settings

_log = logging.getLogger(__name__)


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
        out = await handler(payload)
        await mark_job_finished(job_id, success=True, status="complete", result=out)
    except Exception as e:
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
    _log.info(
        "SQLite job worker started (poll=%ss, stale_running=%ss)",
        poll,
        settings.queue_worker_stale_running_seconds,
    )
    while True:
        row = await claim_next_job_row()
        if row is None:
            await asyncio.sleep(poll)
            continue
        job_id, kind, payload = row
        await _run_one(job_id, kind, payload)


def main() -> None:
    try:
        asyncio.run(run_worker_forever())
    except KeyboardInterrupt:
        _log.info("worker stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
