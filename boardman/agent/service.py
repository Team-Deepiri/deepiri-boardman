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
from boardman.settings import settings

logger = logging.getLogger(__name__)


async def run_agent_chat(
    session: AsyncSession,
    *,
    message: str,
    session_id: Optional[str],
    repo: Optional[str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    allow_writes: bool = False,
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
    else:
        ag.last_active = datetime.utcnow()
        if repo and not ag.repo:
            ag.repo = repo

    history_msgs: List[AgentMessage] = sorted(ag.messages, key=lambda m: m.id)[-settings.agent_max_history :]

    reply: str
    if settings.agent_langchain_tools:
        try:
            lc_hist = db_messages_to_langchain(history_msgs)
            extra = (
                f"\n\n## Tool policy\nPlaky **write** tools (create/update/comment/subtask) are "
                f"**{'ENABLED' if allow_writes else 'OFF'}**. "
                "If OFF, use only list/get and GitHub/repo read tools; tell the user to pass allow_writes to enable mutations."
            )
            if repo:
                extra += f"\n## Repo context\n`{repo}`"
            reply = await run_tool_agent(
                message,
                chat_history=lc_hist,
                allow_writes=allow_writes,
                system_extra=extra,
            )
        except Exception as e:
            logger.warning("LangChain tool agent failed, using plain chat: %s", e)
            llm_messages = _plain_messages(message, repo, history_msgs)
            reply = await chat_complete(llm_messages, provider=provider, model=model)
    else:
        llm_messages = _plain_messages(message, repo, history_msgs)
        reply = await chat_complete(llm_messages, provider=provider, model=model)

    session.add(AgentMessage(session_pk=ag.id, role="user", content=message))
    session.add(AgentMessage(session_pk=ag.id, role="assistant", content=reply))
    await session.flush()

    return reply, sid


def _plain_messages(
    message: str, repo: Optional[str], history_msgs: List[AgentMessage]
) -> List[Dict[str, str]]:
    llm_messages: List[Dict[str, str]] = [{"role": "system", "content": BOARD_MANAGER_SYSTEM}]
    if repo:
        llm_messages[0]["content"] += f"\n\n## Current repo context\nThe user is working with: `{repo}`."
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
