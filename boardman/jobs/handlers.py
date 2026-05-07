"""Registered job kinds for `BackgroundJob` (SQLite worker)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def boardman_agent_chat_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Background agent turn: same logic as POST /agent/chat (commits session)."""
    from boardman.agent.service import run_agent_chat
    from boardman.database.session import async_session
    from boardman.plaky.placement import plaky_placement_context

    message = str(payload.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}

    bid = payload.get("plaky_board_id")
    gid = payload.get("plaky_group_id")
    bs = str(bid).strip() if bid else None
    gs = str(gid).strip() if gid else None
    if bs == "":
        bs = None
    if gs == "":
        gs = None

    async with plaky_placement_context(bs, gs), async_session() as session:
        try:
            reply, sid = await run_agent_chat(
                session,
                message=message,
                session_id=payload.get("session_id"),
                repo=payload.get("repo"),
                provider=payload.get("provider"),
                model=payload.get("model"),
                allow_writes=bool(payload.get("allow_writes")),
                use_tools=bool(payload.get("use_tools")),
                plaky_board_id=bs,
                plaky_group_id=gs,
            )
            await session.commit()
            return {"ok": True, "reply": reply, "session_id": sid}
        except Exception as e:
            logger.exception("boardman_agent_chat_job failed")
            await session.rollback()
            return {"ok": False, "error": str(e)}


async def plaky_reorder_group_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Background: sort Plaky group items (completed last)."""
    from boardman.plaky.client import PlakyClient
    from boardman.services.plaky_group_reorder import reorder_group_completed_last

    bid = str(payload.get("board_id") or "").strip()
    gid = str(payload.get("group_id") or "").strip()
    if not bid or not gid:
        return {"ok": False, "error": "board_id and group_id required"}
    async with PlakyClient() as plaky:
        return await reorder_group_completed_last(plaky, bid, gid)


JOB_HANDLERS: dict[str, JobHandler] = {
    "boardman_agent_chat_job": boardman_agent_chat_job,
    "plaky_reorder_group_job": plaky_reorder_group_job,
}
