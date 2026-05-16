"""Agent chat with DB-backed session history + optional LangChain tools."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boardman.agent.memory_store import db_messages_to_langchain
from boardman.agent.plaky_prompt_extra import plaky_placement_markdown
from boardman.agent.prompts import (
    AGENT_TOOL_FINAL_REPLY_HINT,
    BOARD_MANAGER_SYSTEM,
    PLAIN_CHAT_TOOLS_DISABLED_RUNTIME,
    TASK_CREATION_WORKFLOW,
)
from boardman.agent.runner import iter_tool_agent, run_tool_agent
from boardman.agent.task_draft import format_task_draft_for_prompt, load_task_draft
from boardman.agent.tool_context import agent_tool_context
from boardman.database.models import AgentMessage, AgentSession
from boardman.llm.completion import chat_complete, chat_complete_stream
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.settings import settings

logger = logging.getLogger(__name__)


ErrorCategory = Literal[
    "model_missing",
    "auth",
    "rate_limited",
    "timeout",
    "connectivity",
    "bad_request",
    "upstream_http",
    "unknown",
]


def _normalize_provider(provider: str | None) -> str:
    p = (provider or settings.llm_provider or "ollama").strip().lower()
    aliases = {
        "gpt": "openai",
        "google": "gemini",
        "claude": "anthropic",
        "or": "openrouter",
    }
    return aliases.get(p, p)


def _default_model_for_provider(provider: str) -> str:
    if provider == "anthropic":
        return "claude-sonnet-4-20250514"
    if provider == "openai":
        return "gpt-4o-mini"
    if provider == "openrouter":
        return "anthropic/claude-3.5-sonnet"
    if provider == "gemini":
        return "gemini-2.0-flash"
    if provider == "ollama":
        explicit = (settings.llm_model or "").strip()
        if explicit:
            return explicit
        try:
            from boardman.llm.ollama_autodetect import effective_ollama_model

            return effective_ollama_model(None)
        except Exception:
            return "auto-selected from Ollama"
    return (settings.llm_model or "").strip() or "unspecified"


def _resolve_llm_context(provider: str | None, model: str | None, *, use_tools: bool) -> tuple[str, str]:
    prov = _normalize_provider(provider)
    mdl = (model or "").strip() or (settings.llm_model or "").strip() or _default_model_for_provider(prov)
    return prov, mdl


def _classify_llm_error(exc: BaseException, *, provider: str) -> ErrorCategory:
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            st = exc.response.status_code
            low = ((exc.response.text or "") + " " + str(exc)).lower()
            if st == 404 and (provider != "ollama" or ("model" in low or "/api/chat" in low)):
                return "model_missing"
            if st in (401, 403):
                return "auth"
            if st == 429:
                return "rate_limited"
            if st == 400:
                return "bad_request"
            return "upstream_http"
        if isinstance(exc, httpx.ReadTimeout | httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, (httpx.ConnectError, OSError)):
            return "connectivity"
    except Exception:
        pass

    mod = (getattr(type(exc), "__module__", "") or "").lower()
    if type(exc).__name__ == "ResponseError" and "ollama" in mod:
        low = str(exc).lower()
        if "not found" in low or "model" in low:
            return "model_missing"
        return "upstream_http"

    low = str(exc).lower()
    if "not found" in low and "model" in low:
        return "model_missing"
    return "unknown"


def _provider_hint(provider: str, category: ErrorCategory, model: str) -> str:
    if provider == "ollama":
        if category == "model_missing":
            return (
                "Run `docker compose exec ollama ollama list` (or `ollama list`) and pull a valid tag, "
                f"for example: `docker compose exec ollama ollama pull {model}`. "
                "If **LLM_MODEL** is unset, Boardman auto-selects from `/api/tags`."
            )
        if category == "connectivity":
            return (
                f"Cannot reach Ollama at **{settings.ollama_base_url}**. "
                "If using Docker, set **OLLAMA_BASE_URL=http://ollama:11434** in container env."
            )
        if category == "timeout":
            return (
                "Ollama timed out. Try a smaller model, keep model warm, or raise "
                "**OLLAMA_READ_TIMEOUT_SECONDS**."
            )
        return "Check **OLLAMA_BASE_URL** and confirm Ollama is running."

    if provider == "openrouter":
        if category == "auth":
            return "Verify **OPENROUTER_API_KEY** is set and valid."
        if category == "model_missing":
            return (
                "Use provider-prefixed model IDs (for example `anthropic/claude-3.5-sonnet`) and verify "
                "the model is available for your OpenRouter account."
            )
        if category == "rate_limited":
            return "OpenRouter rate-limited the request. Retry later or switch to another available model."
        return "Check **OPENROUTER_API_KEY**, **OPENROUTER_BASE_URL**, and model availability."

    if provider == "openai":
        if category == "auth":
            return "Verify **OPENAI_API_KEY** and account permissions."
        if category == "model_missing":
            return "Confirm the OpenAI model ID is valid and enabled for your account."
        if category == "rate_limited":
            return "OpenAI rate-limited the request. Retry later or lower request volume."
        return "Check **OPENAI_API_KEY** and model access permissions."

    if provider == "anthropic":
        if category == "auth":
            return "Verify **ANTHROPIC_API_KEY** is set and valid."
        if category == "model_missing":
            return "Confirm the Anthropic model ID is valid for your account."
        if category == "rate_limited":
            return "Anthropic rate-limited the request. Retry later."
        return "Check **ANTHROPIC_API_KEY** and model availability."

    if provider == "gemini":
        if category == "auth":
            return "Verify **GEMINI_API_KEY** is set and valid."
        if category == "model_missing":
            return "Confirm the Gemini model name is valid and available for your project."
        if category == "rate_limited":
            return "Gemini rate-limited the request. Retry later."
        return "Check **GEMINI_API_KEY** and model availability."

    return "Check LLM provider configuration and credentials."


def _format_llm_failure(exc: BaseException, *, provider: str, model: str) -> str:
    """User-visible provider-aware message for LLM failures."""
    category = _classify_llm_error(exc, provider=provider)
    base = (str(exc) or type(exc).__name__).strip()
    hint = _provider_hint(provider, category, model)
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            st = exc.response.status_code
            snippet = (exc.response.text or "").strip().replace("\n", " ")[:400]
            base = f"HTTP {st} from the model API"
            if snippet:
                base += f": {snippet}"
    except Exception:
        pass
    return (
        "I could not get a reply from the language model.\n\n"
        f"**Provider:** `{provider}`\n"
        f"**Model:** `{model}`\n"
        f"**What went wrong:** {base}\n\n"
        f"**Fix:** {hint}"
    )


async def _load_draft_markdown(session: AsyncSession, agent_session_pk: int | None) -> str:
    if not agent_session_pk:
        return ""
    draft = await load_task_draft(session, agent_session_pk)
    return format_task_draft_for_prompt(draft)


def _plain_chat_extra_suffix(draft_md: str, intake_extra: str, *, use_tools: bool) -> str:
    """Append runtime override when multi-step tools are off so the model does not fake tool JSON."""
    base = draft_md + intake_extra
    if settings.agent_langchain_tools and use_tools:
        return base
    return base + PLAIN_CHAT_TOOLS_DISABLED_RUNTIME


async def _safe_plain_chat(
    *,
    message: str,
    repo: str | None,
    history_msgs: list[AgentMessage],
    plaky_suffix: str,
    provider: str | None,
    model: str | None,
    resolved_provider: str,
    resolved_model: str,
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
        return _format_llm_failure(e, provider=resolved_provider, model=resolved_model)


async def _plaky_system_suffix(
    plaky_board_id: str | None,
    plaky_group_id: str | None,
) -> str:
    out = plaky_placement_markdown(plaky_board_id, plaky_group_id)
    bid = (plaky_board_id or "").strip()
    if bid:
        bundle = await fetch_board_schema_bundle(bid)
        out += bundle.get("markdown") or ""
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
    resolved_provider, resolved_model = _resolve_llm_context(provider, model, use_tools=use_tools)
    use_lc = bool(settings.agent_langchain_tools and use_tools)
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
                f"**{'ENABLED' if allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations.\n"
                "If ON: you **must** run **plaky_board_schema** (and **plaky_list_workspace_users** for assignees) before "
                "**plaky_create_task** / **plaky_patch_item_fields** when field keys are not already explicit in context; "
                "the API rejects invented field keys."
            )
            if repo:
                extra += f"\n## Repo context\n`{repo}`"
            extra += plaky_suffix
            extra += draft_md + intake_extra + AGENT_TOOL_FINAL_REPLY_HINT
            async with agent_tool_context(session, ag.id, plaky_board_id, plaky_group_id):
                reply = await run_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=allow_writes,
                    system_extra=extra,
                    request_model=model,
                )
        except Exception as e:
            logger.warning("LangChain tool agent failed, using plain chat: %s", e, exc_info=True)
            reply = await _safe_plain_chat(
                message=message,
                repo=repo,
                history_msgs=history_msgs,
                plaky_suffix=plaky_suffix,
                provider=provider,
                model=model,
                resolved_provider=resolved_provider,
                resolved_model=resolved_model,
                extra_system_suffix=_plain_chat_extra_suffix(
                    draft_md, intake_extra, use_tools=False
                ),
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
            resolved_provider=resolved_provider,
            resolved_model=resolved_model,
            extra_system_suffix=_plain_chat_extra_suffix(
                draft_md, intake_extra, use_tools=use_tools
            ),
        )

    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))
    session.add(AgentMessage(session_pk=ag.id, role="assistant", content=reply))
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
    resolved_provider, resolved_model = _resolve_llm_context(provider, model, use_tools=use_tools)
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
            extra += draft_md + intake_extra + AGENT_TOOL_FINAL_REPLY_HINT
            async with agent_tool_context(session, ag.id, plaky_board_id, plaky_group_id):
                async for chunk in iter_tool_agent(
                    message,
                    chat_history=lc_hist,
                    allow_writes=allow_writes,
                    system_extra=extra,
                    request_model=model,
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
                extra_system_suffix=_plain_chat_extra_suffix(
                    draft_md, intake_extra, use_tools=use_tools
                ),
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
        err = _format_llm_failure(e, provider=resolved_provider, model=resolved_model)
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
