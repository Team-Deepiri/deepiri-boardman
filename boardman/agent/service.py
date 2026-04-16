"""Agent chat with DB-backed session history + optional LangChain tools."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boardman.agent.memory_store import db_messages_to_langchain
from boardman.agent.plaky_prompt_extra import plaky_placement_markdown
from boardman.agent.prompts import BOARD_MANAGER_SYSTEM, TASK_CREATION_WORKFLOW
from boardman.agent.runner import iter_tool_agent, run_tool_agent
from boardman.agent.guardrails import has_confirm_token, looks_like_board_organize_request
from boardman.agent.task_draft import format_task_draft_for_prompt, load_task_draft
from boardman.agent.tool_context import agent_tool_context
from boardman.database.models import AgentMessage, AgentSession
from boardman.llm.completion import chat_complete, chat_complete_stream
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.settings import settings

logger = logging.getLogger(__name__)


def _is_ollama_model_missing_error(exc: BaseException) -> bool:
    """True when Ollama has no matching model (LangChain ResponseError or HTTP 404 on /api/chat)."""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return True
    except Exception:
        pass
    mod = getattr(type(exc), "__module__", "") or ""
    if type(exc).__name__ == "ResponseError" and "ollama" in mod:
        return True
    low = str(exc).lower()
    return "not found" in low and "model" in low


def _ollama_model_label_for_errors() -> str:
    explicit = (settings.llm_model or "").strip()
    if explicit:
        return explicit
    try:
        from boardman.llm.ollama_autodetect import effective_ollama_model

        return effective_ollama_model(None)
    except Exception:
        return "auto-selected from Ollama"


def _ollama_model_missing_user_reply() -> str:
    m = _ollama_model_label_for_errors()
    return (
        "Ollama rejected the model **`"
        + m
        + "`** (missing or wrong tag).\n\n"
        "**Fix:** `docker compose exec ollama ollama list` then either pull that tag "
        f"(`docker compose exec ollama ollama pull {m}`) or pull any model you want; "
        "with **LLM_MODEL** unset, Boardman picks one from `/api/tags` automatically.\n"
    )


def _format_llm_failure(exc: BaseException) -> str:
    """User-visible message when Ollama / LLM HTTP fails (avoids opaque HTTP 500 in the UI)."""
    base = (str(exc) or type(exc).__name__).strip()
    hint = ""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            st = exc.response.status_code
            snippet = (exc.response.text or "").strip().replace("\n", " ")[:400]
            base = f"HTTP {st} from the model API"
            if snippet:
                base += f": {snippet}"
            if st == 404:
                hint = (
                    "\n\nThat model is not available in Ollama. "
                    "Run `docker compose exec ollama ollama list` (or `ollama list` on the host). "
                    "Pull a model if the list is empty. With **LLM_MODEL** unset, Boardman auto-selects from the list."
                )
        elif isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, OSError)):
            hint = (
                f"\n\nCannot reach Ollama at **{settings.ollama_base_url}**. "
                "If Boardman runs in Docker, set **OLLAMA_BASE_URL=http://ollama:11434** (service name) "
                "and ensure the **ollama** container is running."
            )
    except Exception:
        pass
    return (
        "I could not get a reply from the language model.\n\n"
        f"**What went wrong:** {base}{hint}\n\n"
        "Check **OLLAMA_BASE_URL** and that Ollama is running (optional **LLM_MODEL** overrides auto-pick)."
    )


async def _load_draft_markdown(session: AsyncSession, agent_session_pk: int | None) -> str:
    if not agent_session_pk:
        return ""
    draft = await load_task_draft(session, agent_session_pk)
    return format_task_draft_for_prompt(draft)


async def _safe_plain_chat(
    *,
    message: str,
    repo: str | None,
    history_msgs: list[AgentMessage],
    plaky_suffix: str,
    provider: str | None,
    model: str | None,
    extra_system_suffix: str = "",
) -> str:
    try:
        llm_messages = _build_plain_llm_messages(
            message,
            repo,
            history_msgs,
            plaky_suffix,
            extra_system_suffix=extra_system_suffix,
        )
        return await chat_complete(llm_messages, provider=provider, model=model)
    except Exception as e:
        logger.exception("Plain chat (Ollama/direct LLM) failed")
        return _format_llm_failure(e)


async def _plaky_system_suffix(
    plaky_board_id: str | None,
    plaky_group_id: str | None,
) -> str:
    out = plaky_placement_markdown(plaky_board_id, plaky_group_id)
    bid = (plaky_board_id or "").strip()
    if bid:
        try:
            bundle = await fetch_board_schema_bundle(bid)
            out += bundle.get("markdown") or ""
        except Exception as e:
            logger.warning("Could not load Plaky board schema bundle for %s: %s", bid, e)
            out += (
                f"\n\n## Current Plaky board schema (from API)\n"
                f"**Board id:** `{bid}`\n"
                "Schema could not be loaded right now; continue using known placement and refresh schema when possible.\n"
            )
    return out


async def run_agent_chat(
    session: AsyncSession,
    *,
    message: str,
    session_id: str | None,
    repo: str | None,
    provider: str | None = None,
    model: str | None = None,
    allow_writes: bool = False,
    use_tools: bool = False,
    plaky_board_id: str | None = None,
    plaky_group_id: str | None = None,
) -> tuple[str, str]:
    """Persist user message, call LLM (tool agent or plain chat), persist assistant reply."""
    sid = session_id or str(uuid.uuid4())

    q = (
        select(AgentSession)
        .where(AgentSession.session_id == sid)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag: AgentSession | None = res.scalar_one_or_none()

    if ag is None:
        ag = AgentSession(
            session_id=sid,
            repo=repo,
            prompt_version=settings.prompt_version,
            created_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        )
        session.add(ag)
        await session.flush()
        history_msgs: list[AgentMessage] = []
    else:
        ag.last_active = datetime.utcnow()
        if repo and not ag.repo:
            ag.repo = repo
        history_msgs = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    intake_extra = TASK_CREATION_WORKFLOW
    draft_md, plaky_suffix = await asyncio.gather(
        _load_draft_markdown(session, ag.id),
        _plaky_system_suffix(plaky_board_id, plaky_group_id),
    )

    reply: str
    assistant_tool_calls_json: Optional[str] = None
    use_lc = bool(settings.agent_langchain_tools and use_tools)
    effective_allow_writes = allow_writes
    preview_notice = ""
    if (
        use_lc
        and allow_writes
        and settings.agent_require_confirm_bulk
        and looks_like_board_organize_request(message)
        and not has_confirm_token(message)
    ):
        effective_allow_writes = False
        preview_notice = (
            "I detected a board-organization/bulk-change request. I ran in preview mode (read-only) first.\n\n"
            "Reply with **confirm** (or **yes, apply**) in your next message to enable write tools for apply.\n"
        )
    if use_lc:
        try:
            logger.info(
                "Agent chat: LangChain tool path (session_id=%s, allow_writes=%s, repo=%s)",
                sid,
                allow_writes,
                repo or "",
            )
            lc_hist = db_messages_to_langchain(history_msgs)
            extra = (
                f"\n\n## Tool policy\nPlaky **write** tools (create/update/comment/subtask) are "
                f"**{'ENABLED' if effective_allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations.\n"
                "If ON: you **must** run **plaky_board_schema** (and **plaky_list_workspace_users** for assignees) before "
                "**plaky_create_task** / **plaky_patch_item_fields** when field keys are not already explicit in context; "
                "the API rejects invented field keys."
            )
            if repo:
                extra += f"\n## Repo context\n`{repo}`"
            extra += plaky_suffix
            extra += draft_md + intake_extra
            async with agent_tool_context(session, ag.id, plaky_board_id, plaky_group_id):
                tool_out = await run_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=effective_allow_writes,
                    system_extra=extra,
                    return_trace=True,
                )
                if isinstance(tool_out, tuple):
                    reply, traces = tool_out
                    if traces:
                        assistant_tool_calls_json = json.dumps(traces, default=str)[:64000]
                else:
                    reply = str(tool_out)
                if preview_notice:
                    reply = preview_notice + "\n" + reply
        except Exception as e:
            if _is_ollama_model_missing_error(e):
                logger.warning("LangChain tool agent failed (Ollama model missing): %s", e)
                reply = _ollama_model_missing_user_reply()
                assistant_tool_calls_json = json.dumps(
                    [{"tool_name": "agent_runtime", "status": "error", "result_summary": str(e)[:500]}]
                )
            else:
                logger.warning("LangChain tool agent failed, using plain chat: %s", e, exc_info=True)
                assistant_tool_calls_json = json.dumps(
                    [{"tool_name": "agent_runtime", "status": "error", "result_summary": str(e)[:500]}]
                )
                reply = await _safe_plain_chat(
                    message=message,
                    repo=repo,
                    history_msgs=history_msgs,
                    plaky_suffix=plaky_suffix,
                    provider=provider,
                    model=model,
                    extra_system_suffix=draft_md + intake_extra,
                )
    else:
        logger.info(
            "Agent chat: plain LLM path (single completion; session_id=%s use_tools=%s)",
            sid,
            use_tools,
        )
        reply = await _safe_plain_chat(
            message=message,
            repo=repo,
            history_msgs=history_msgs,
            plaky_suffix=plaky_suffix,
            provider=provider,
            model=model,
            extra_system_suffix=draft_md + intake_extra,
        )

    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))
    session.add(
        AgentMessage(
            session_pk=ag.id,
            role="assistant",
            content=reply,
            tool_calls_json=assistant_tool_calls_json,
        )
    )
    await session.flush()

    return reply, sid


def _sse_event(obj: Any) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def iter_agent_chat_sse(
    session: AsyncSession,
    *,
    message: str,
    session_id: str | None,
    repo: str | None,
    provider: str | None = None,
    model: str | None = None,
    allow_writes: bool = False,
    use_tools: bool = False,
    plaky_board_id: str | None = None,
    plaky_group_id: str | None = None,
) -> AsyncIterator[bytes]:
    """
    SSE frames with JSON payloads: session → token (many) → done | error.
    Supports both plain chat and multi-step tool agent (streaming).
    """
    sid = session_id or str(uuid.uuid4())

    q = (
        select(AgentSession)
        .where(AgentSession.session_id == sid)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag: AgentSession | None = res.scalar_one_or_none()

    if ag is None:
        ag = AgentSession(
            session_id=sid,
            repo=repo,
            prompt_version=settings.prompt_version,
            created_at=datetime.utcnow(),
            last_active=datetime.utcnow(),
        )
        session.add(ag)
        await session.flush()
        history_msgs: list[AgentMessage] = []
    else:
        ag.last_active = datetime.utcnow()
        if repo and not ag.repo:
            ag.repo = repo
        history_msgs = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    intake_extra = TASK_CREATION_WORKFLOW
    draft_md, plaky_suffix = await asyncio.gather(
        _load_draft_markdown(session, ag.id),
        _plaky_system_suffix(plaky_board_id, plaky_group_id),
    )

    yield _sse_event({"type": "session", "session_id": sid})

    parts: list[str] = []
    use_lc = bool(settings.agent_langchain_tools and use_tools)

    try:
        if use_lc:
            logger.info("Agent chat stream: LangChain tool path (session_id=%s)", sid)
            lc_hist = db_messages_to_langchain(history_msgs)
            extra = (
                f"\n\n## Tool policy\nPlaky **write** tools (create/update/comment/subtask) are "
                f"**{'ENABLED' if allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations.\n"
                "If ON: you **must** run **plaky_board_schema** (and **plaky_list_workspace_users** for assignees) before "
                "**plaky_create_task** / **plaky_patch_item_fields** when field keys are not already explicit in context; "
                "the API rejects invented field keys."
            )
            if repo:
                extra += f"\n## Repo context\n`{repo}`"
            extra += plaky_suffix
            extra += draft_md + intake_extra
            async with agent_tool_context(session, ag.id, plaky_board_id, plaky_group_id):
                async for chunk in iter_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=allow_writes,
                    system_extra=extra,
                ):
                    if not chunk:
                        continue
                    parts.append(chunk)
                    yield _sse_event({"type": "token", "text": chunk})
        else:
            logger.info("Agent chat stream: plain LLM path (session_id=%s)", sid)
            llm_messages = _build_plain_llm_messages(
                message,
                repo,
                history_msgs,
                plaky_suffix,
                extra_system_suffix=draft_md + intake_extra,
            )
            async for chunk in chat_complete_stream(llm_messages, provider=provider, model=model):
                if not chunk:
                    continue
                parts.append(chunk)
                yield _sse_event({"type": "token", "text": chunk})

        reply = "".join(parts)
        session.add(AgentMessage(session_pk=ag.id, role="user", content=message))
        session.add(AgentMessage(session_pk=ag.id, role="assistant", content=reply))
        await session.flush()
        yield _sse_event({"type": "done"})

    except Exception as e:
        logger.exception("Agent chat stream failed")
        err = _format_llm_failure(e) if not _is_ollama_model_missing_error(e) else _ollama_model_missing_user_reply()
        yield _sse_event({"type": "error", "message": err})


def _build_plain_llm_messages(
    message: str,
    repo: str | None,
    history_msgs: list[AgentMessage],
    plaky_suffix: str,
    extra_system_suffix: str = "",
) -> list[dict[str, str]]:
    llm_messages: list[dict[str, str]] = [{"role": "system", "content": BOARD_MANAGER_SYSTEM}]
    if repo:
        llm_messages[0]["content"] += f"\n\n## Current repo context\nThe user is working with: `{repo}`."
    llm_messages[0]["content"] += plaky_suffix
    llm_messages[0]["content"] += extra_system_suffix
    for m in history_msgs:
        llm_messages.append({"role": m.role, "content": m.content})
    llm_messages.append({"role": "user", "content": message})
    return llm_messages


async def get_session_history(session: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    q = (
        select(AgentSession)
        .where(AgentSession.session_id == session_id)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag = res.scalar_one_or_none()
    if not ag:
        return []
    out: list[dict[str, Any]] = []
    for m in sorted(ag.messages, key=lambda x: x.id):
        out.append(
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )
    return out


async def delete_agent_session(session: AsyncSession, session_id: str) -> bool:
    q = select(AgentSession).where(AgentSession.session_id == session_id)
    res = await session.execute(q)
    ag = res.scalar_one_or_none()
    if not ag:
        return False
    await session.delete(ag)
    return True
