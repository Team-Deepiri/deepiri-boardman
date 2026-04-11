"""LangChain tool-calling agent (optional; falls back if deps/model fail)."""

from __future__ import annotations

import logging
from typing import Any, List

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage

from boardman.agent.prompts import BOARD_MANAGER_SYSTEM
from boardman.agent.tools import build_all_tools
from boardman.llm.factory import get_chat_model
from boardman.settings import settings

logger = logging.getLogger(__name__)

def _recursion_limit() -> int:
    n = int(getattr(settings, "agent_recursion_limit", 22) or 22)
    return max(5, min(80, n))


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(content or "").strip()


def _final_ai_text(messages: list[AnyMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = _message_content_to_text(m.content)
            if text:
                return text
    return ""


async def run_tool_agent(
    user_input: str,
    *,
    chat_history: List[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
) -> str:
    from langchain.agents import create_agent

    llm = get_chat_model()
    tools = build_all_tools(allow_writes=allow_writes)
    verbose = settings.agent_langchain_verbose or logger.isEnabledFor(logging.DEBUG)
    logger.info(
        "LangChain create_agent: %d tools, verbose=%s, provider/model from settings",
        len(tools),
        verbose,
    )

    graph = create_agent(
        llm,
        tools=tools,
        system_prompt=BOARD_MANAGER_SYSTEM + system_extra,
        debug=verbose,
    )
    messages: list[BaseMessage] = list(chat_history) + [HumanMessage(content=user_input)]
    result = await graph.ainvoke(
        {"messages": messages},
        config={"recursion_limit": _recursion_limit()},
    )
    out = _final_ai_text(result.get("messages", []))
    logger.info("LangChain agent finished (output length=%d)", len(out))
    return out or "(No assistant text returned.)"
