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
        except Exception as e:  # noqa: BLE001 - logged and handled
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


async def boardman_repo_scan_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Background: run an LLM repo scan without holding an HTTP request open."""
    from boardman.database.session import async_session
    from boardman.services.scan_handler import run_repo_scan

    repo = str(payload.get("repo") or "").strip()
    if not repo:
        return {"ok": False, "error": "repo required"}

    async with async_session() as session:
        try:
            result = await run_repo_scan(
                session,
                repo,
                dry_run=bool(payload.get("dry_run")),
                provider=payload.get("provider"),
                model=payload.get("model"),
            )
            await session.commit()
            return result
        except Exception as e:  # noqa: BLE001 - logged and handled
            logger.exception("boardman_repo_scan_job failed")
            await session.rollback()
            return {"ok": False, "error": str(e)}


async def boardman_github_webhook_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Process one verified GitHub delivery off the HTTP request path."""
    import asyncio

    from boardman.database.models import GitHubWebhookDelivery
    from boardman.database.session import async_session
    from boardman.routes.github_events import dispatch_github_event
    from boardman.settings import settings

    event_type = str(payload.get("event_type") or "").strip()
    body = payload.get("payload")
    delivery_id = str(payload.get("delivery_id") or "").strip()
    if not event_type or not isinstance(body, dict):
        return {"ok": False, "error": "event_type and payload are required"}

    last_error = ""
    attempts = max(1, int(settings.github_webhook_job_retries or 0) + 1)
    for attempt in range(1, attempts + 1):
        async with async_session() as session:
            try:
                result = await dispatch_github_event(event_type, body, session)
                if not result.get("ok", True):
                    raise RuntimeError(str(result.get("message") or "synchronization failed"))
                if delivery_id:
                    row = await session.get(GitHubWebhookDelivery, delivery_id)
                    if row:
                        row.status = "processed"
                        row.note = f"worker attempt {attempt}"
                await session.commit()
                return {**result, "delivery_id": delivery_id, "attempt": attempt}
            except Exception as exc:  # noqa: BLE001 - graceful degradation
                last_error = str(exc)[:500]
                await session.rollback()
                if delivery_id:
                    row = await session.get(GitHubWebhookDelivery, delivery_id)
                    if row:
                        row.status = "failed" if attempt >= attempts else "processing"
                        row.note = f"worker attempt {attempt}: {last_error}"
                    await session.commit()
        if attempt < attempts:
            await asyncio.sleep(min(2.0 ** (attempt - 1), 8.0))
    raise RuntimeError(last_error or "GitHub webhook synchronization failed")


async def plaky_create_tasks_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a batch of Plaky tasks after the assistant has already replied.

    The reply names what is being created; this does the writing. Whatever actually
    happened (created, already existed, failed) is recorded on the job so a follow-up
    question is answered from the outcome and not from the plan.
    """
    import json as _json

    from boardman.agent.tools.plaky_tools import _plaky_create_tasks
    from boardman.plaky.placement import plaky_placement_context

    rows = payload.get("tasks") or []
    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "no task rows in payload"}
    bid = str(payload.get("board_id") or "").strip()
    gid = str(payload.get("group_id") or "").strip()

    async with plaky_placement_context(bid or None, gid or None):
        raw = await _plaky_create_tasks(
            _json.dumps(rows),
            board_id=bid,
            group_id=gid,
            auto_assign_team=bool(payload.get("auto_assign_team")),
        )
    try:
        out = _json.loads(raw)
    except ValueError:
        return {"ok": False, "error": str(raw)[:500]}
    logger.info(
        "deferred plaky create: %s created, %s already existed, %s failed",
        out.get("created_count"),
        out.get("already_existed_count"),
        out.get("failed_count"),
    )
    return out


async def boardman_repo_refresh_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one repo's cached briefing, off the request path.

    This is the revalidate half of stale-while-revalidate: the question that found a stale
    snapshot was answered from it immediately, and this repairs it for the next one. It is
    also what the periodic sweep calls when it sees a repo has moved on.

    The planning fetch persists through ``get_tool_db_session()``, a ContextVar that is
    unset outside a tool call — without the ``agent_tool_context`` wrapper the fetch
    succeeds, the save silently no-ops, and the snapshot never gets any fresher.
    """
    import json as _json

    from boardman.agent.tool_context import agent_tool_context
    from boardman.agent.tools.github_tools import _github_repo_planning_context_uncached
    from boardman.database.session import async_session

    repo = str(payload.get("repo") or "").strip()
    if "/" not in repo:
        return {"ok": False, "error": "repo must be owner/name"}

    async with async_session() as session:
        async with agent_tool_context(session, 0, None, None):
            raw = await _github_repo_planning_context_uncached(repo)
        await session.commit()

    try:
        out = _json.loads(raw)
    except ValueError:
        return {"ok": False, "repo": repo, "error": str(raw)[:300]}
    logger.info("repo refresh: %s ok=%s", repo, out.get("ok"))
    return {"ok": bool(out.get("ok")), "repo": repo, "source_revision": out.get("source_revision")}


JOB_HANDLERS: dict[str, JobHandler] = {
    "boardman_agent_chat_job": boardman_agent_chat_job,
    "boardman_github_webhook_job": boardman_github_webhook_job,
    "boardman_repo_scan_job": boardman_repo_scan_job,
    "boardman_repo_refresh_job": boardman_repo_refresh_job,
    "plaky_create_tasks_job": plaky_create_tasks_job,
    "plaky_reorder_group_job": plaky_reorder_group_job,
}
