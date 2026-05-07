"""Fire-and-forget side effects from GitHub webhooks (SQLite background jobs)."""

from __future__ import annotations

import logging

from boardman.plaky.client import PlakyClient
from boardman.plaky.task_payload_ids import placement_ids_from_plaky_task
from boardman.settings import settings

_log = logging.getLogger(__name__)


async def maybe_enqueue_plaky_reorder_after_task(plaky: PlakyClient, task_id: str) -> None:
    """After a Plaky item status change, optionally reorder that item's group (board/group from the task)."""
    if not settings.plaky_reorder_after_status_change:
        return
    tid = (task_id or "").strip()
    if not tid:
        return
    try:
        got = await plaky.get_task(tid)
        task = got.get("task") if isinstance(got, dict) else None
        bid, gid = placement_ids_from_plaky_task(task if isinstance(task, dict) else None)
        if not bid or not gid:
            return
        from boardman.broker.job_queue import get_job_queue

        await get_job_queue().enqueue_job(
            "plaky_reorder_group_job",
            {"board_id": bid, "group_id": gid},
        )
    except Exception:
        _log.exception("enqueue plaky_reorder_group_job failed")
