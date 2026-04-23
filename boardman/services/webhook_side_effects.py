"""Fire-and-forget side effects from GitHub webhooks (SQLite background jobs)."""

from __future__ import annotations

import logging

from boardman.settings import settings

_log = logging.getLogger(__name__)


async def maybe_enqueue_plaky_reorder_job() -> None:
    if not settings.plaky_reorder_after_status_change:
        return
    bid = (settings.plaky_default_board_id or "").strip()
    gid = (settings.plaky_default_group_id or "").strip()
    if not bid or not gid:
        return
    try:
        from boardman.broker.job_queue import get_job_queue

        await get_job_queue().enqueue_job(
            "plaky_reorder_group_job",
            {"board_id": bid, "group_id": gid},
        )
    except Exception:
        _log.exception("enqueue plaky_reorder_group_job failed")
