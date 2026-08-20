"""Run a queued job in this process, right now, without waiting for the worker.

Board writes are slow (Plaky shapes bursts, five tasks take tens of seconds) but the
person asking does not need to wait for them. Boardman answers with what it decided and
why, and the writes continue behind the reply — the board catches up seconds later.

The durable `background_jobs` row is still written first, so the work is recoverable and
its outcome is inspectable at GET /agent/jobs/{id}. `claim_job_by_id` is what stops the
standalone worker and this in-process runner from both doing it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from boardman.broker.job_queue import claim_job_by_id, get_job_queue, mark_job_finished

logger = logging.getLogger(__name__)

_running: set[asyncio.Task[Any]] = set()


async def _run_claimed(job_id: str) -> None:
    from boardman.jobs.handlers import JOB_HANDLERS

    claimed = await claim_job_by_id(job_id)
    if claimed is None:
        return  # the standalone worker got there first
    _jid, kind, payload = claimed
    handler = JOB_HANDLERS.get(kind)
    if handler is None:
        await mark_job_finished(
            job_id, success=False, status="incomplete", result={"error": f"no handler for {kind}"}
        )
        return
    try:
        out = await handler(payload)
        await mark_job_finished(
            job_id, success=bool(out.get("ok", True)), status="complete", result=out
        )
    except Exception as exc:  # noqa: BLE001 - a background failure must be recorded, not raised
        logger.exception("deferred job %s (%s) failed", job_id, kind)
        await mark_job_finished(
            job_id, success=False, status="incomplete", result={"error": str(exc)[:500]}
        )


async def enqueue_and_run_soon(kind: str, payload: dict[str, Any]) -> str:
    """Persist the job, start it in the background, and return its id immediately."""
    job = await get_job_queue().enqueue_job(kind, payload)
    task = asyncio.create_task(_run_claimed(job.job_id), name=f"deferred:{kind}")
    # Hold a reference: an un-awaited task can otherwise be garbage collected mid-flight.
    _running.add(task)
    task.add_done_callback(_running.discard)
    return job.job_id


async def wait_for_deferred(timeout: float = 10.0) -> None:
    """Await in-flight deferred work.

    Tests need this so a background write cannot outlive the fixture's database, and
    a graceful shutdown can use it to let queued board writes finish.
    """
    if not _running:
        return
    await asyncio.wait(set(_running), timeout=timeout)
