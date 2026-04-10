"""Agent chat with DB-backed session history + optional LangChain tools."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from boardman.agent.memory_store import db_messages_to_langchain
from boardman.agent.prompts import BOARD_MANAGER_SYSTEM
from boardman.agent.runner import run_tool_agent
from boardman.database.models import AgentMessage, AgentSession
from boardman.llm.completion import chat_complete
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.settings import settings

logger = logging.getLogger(__name__)


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
                    "\n\nThis often means **LLM_MODEL** does not match a pulled Ollama model. "
                    "Run `ollama list` and set **LLM_MODEL** to the same name (e.g. `qwen2.5:7b`)."
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
        "Check **OLLAMA_BASE_URL**, **LLM_MODEL**, and that Ollama is running."
    )


async def _safe_plain_chat(
    *,
    message: str,
    repo: Optional[str],
    history_msgs: List[AgentMessage],
    plaky_board_id: Optional[str],
    provider: Optional[str],
    model: Optional[str],
) -> str:
    try:
        llm_messages = await _plain_messages_async(message, repo, history_msgs, plaky_board_id)
        return await chat_complete(llm_messages, provider=provider, model=model)
    except Exception as e:
        logger.exception("Plain chat (Ollama/direct LLM) failed")
        return _format_llm_failure(e)


async def run_agent_chat(
    session: AsyncSession,
    *,
    message: str,
    session_id: Optional[str],
    repo: Optional[str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    allow_writes: bool = False,
    plaky_board_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Persist user message, call LLM (tool agent or plain chat), persist assistant reply."""
    sid = session_id or str(uuid.uuid4())

    q = (
        select(AgentSession)
        .where(AgentSession.session_id == sid)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag: Optional[AgentSession] = res.scalar_one_or_none()

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
        history_msgs: List[AgentMessage] = []
    else:
        ag.last_active = datetime.utcnow()
        if repo and not ag.repo:
            ag.repo = repo
        history_msgs = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    reply: str
    if settings.agent_langchain_tools:
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
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations."
            )
            if repo:
                extra += f"\n## Repo context\n`{repo}`"
            bid = (plaky_board_id or "").strip()
            if bid:
                bundle = await fetch_board_schema_bundle(bid)
                extra += bundle.get("markdown") or ""
            reply = await run_tool_agent(
                message,
                chat_history=lc_hist,
                allow_writes=allow_writes,
                system_extra=extra,
            )
        except Exception as e:
            logger.warning("LangChain tool agent failed, using plain chat: %s", e, exc_info=True)
            reply = await _safe_plain_chat(
                message=message,
                repo=repo,
                history_msgs=history_msgs,
                plaky_board_id=plaky_board_id,
                provider=provider,
                model=model,
            )
    else:
        logger.info(
            "Agent chat: plain LLM path (AGENT_LANGCHAIN_TOOLS off; session_id=%s)",
            sid,
        )
        reply = await _safe_plain_chat(
            message=message,
            repo=repo,
            history_msgs=history_msgs,
            plaky_board_id=plaky_board_id,
            provider=provider,
            model=model,
        )

    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))
    session.add(AgentMessage(session_pk=ag.id, role="assistant", content=reply))
    await session.flush()

    return reply, sid


async def _plain_messages_async(
    message: str,
    repo: Optional[str],
    history_msgs: List[AgentMessage],
    plaky_board_id: Optional[str],
) -> List[Dict[str, str]]:
    llm_messages: List[Dict[str, str]] = [{"role": "system", "content": BOARD_MANAGER_SYSTEM}]
    if repo:
        llm_messages[0]["content"] += f"\n\n## Current repo context\nThe user is working with: `{repo}`."
    bid = (plaky_board_id or "").strip()
    if bid:
        bundle = await fetch_board_schema_bundle(bid)
        llm_messages[0]["content"] += bundle.get("markdown") or ""
    for m in history_msgs:
        llm_messages.append({"role": m.role, "content": m.content})
    llm_messages.append({"role": "user", "content": message})
    return llm_messages


async def get_session_history(session: AsyncSession, session_id: str) -> List[Dict[str, Any]]:
    q = (
        select(AgentSession)
        .where(AgentSession.session_id == session_id)
        .options(selectinload(AgentSession.messages))
    )
    res = await session.execute(q)
    ag = res.scalar_one_or_none()
    if not ag:
        return []
    out: List[Dict[str, Any]] = []
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
