"""Map DB agent messages ↔ LangChain message list."""

from __future__ import annotations

from typing import List

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from boardman.database.models import AgentMessage


def db_messages_to_langchain(rows: List[AgentMessage]) -> List[BaseMessage]:
    out: List[BaseMessage] = []
    for m in rows:
        if m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
    return out
