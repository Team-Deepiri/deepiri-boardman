"""LangChain tool-calling agent (optional; falls back if deps/model fail)."""

from __future__ import annotations

import logging
from typing import List

from langchain_core.messages import BaseMessage

from boardman.agent.prompts import BOARD_MANAGER_SYSTEM
from boardman.agent.tools import build_all_tools
from boardman.llm.factory import get_chat_model

logger = logging.getLogger(__name__)


async def run_tool_agent(
    user_input: str,
    *,
    chat_history: List[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
) -> str:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    llm = get_chat_model()
    tools = build_all_tools(allow_writes=allow_writes)

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", BOARD_MANAGER_SYSTEM + system_extra),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        max_iterations=14,
        handle_parsing_errors=True,
        return_intermediate_steps=False,
    )
    result = await executor.ainvoke({"input": user_input, "chat_history": chat_history})
    if isinstance(result, dict) and "output" in result:
        return str(result["output"])
    return str(result)
