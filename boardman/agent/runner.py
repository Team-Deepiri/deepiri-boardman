"""LangChain tool-calling agent (optional; falls back if deps/model fail)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage

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


def _tool_call_records(m: AIMessage) -> List[Dict[str, Any]]:
    tcalls: List[Dict[str, Any]] = []
    raw = getattr(m, "tool_calls", None)
    if isinstance(raw, list) and raw:
        for t in raw:
            if isinstance(t, dict):
                tcalls.append(t)
    if tcalls:
        return tcalls
    akw = getattr(m, "additional_kwargs", None)
    if isinstance(akw, dict):
        raw2 = akw.get("tool_calls")
        if isinstance(raw2, list):
            for t in raw2:
                if isinstance(t, dict):
                    tcalls.append(t)
    return tcalls


def _summarize_tool_text(content: Any) -> str:
    txt = _message_content_to_text(content)
    return txt[:500] if txt else ""


def _normalize_trace_args(args: Any) -> Any:
    """Coerce Ollama-style JSON string arguments into a dict when possible."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return args


def _extract_tool_trace(messages: List[AnyMessage]) -> List[Dict[str, Any]]:
    traces: List[Dict[str, Any]] = []
    index_by_id: Dict[str, int] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for t in _tool_call_records(m):
                tid = str(t.get("id") or t.get("tool_call_id") or "")
                name = (
                    str(t.get("name") or "")
                    or str((t.get("function") or {}).get("name") or "")
                    or "unknown_tool"
                )
                args = t.get("args")
                if args is None:
                    args = (t.get("function") or {}).get("arguments")
                args = _normalize_trace_args(args)
                row = {
                    "tool_name": name,
                    "tool_call_id": tid or None,
                    "args": args,
                    "status": "called",
                    "result_summary": "",
                }
                traces.append(row)
                if tid:
                    index_by_id[tid] = len(traces) - 1
        elif isinstance(m, ToolMessage):
            tid = str(getattr(m, "tool_call_id", "") or "")
            name = str(getattr(m, "name", "") or "unknown_tool")
            summary = _summarize_tool_text(m.content)
            if tid and tid in index_by_id:
                traces[index_by_id[tid]]["status"] = "ok"
                traces[index_by_id[tid]]["result_summary"] = summary
                if name and traces[index_by_id[tid]]["tool_name"] == "unknown_tool":
                    traces[index_by_id[tid]]["tool_name"] = name
            else:
                traces.append(
                    {
                        "tool_name": name,
                        "tool_call_id": tid or None,
                        "args": None,
                        "status": "ok",
                        "result_summary": summary,
                    }
                )
    return traces[:120]


async def run_tool_agent(
    user_input: str,
    *,
    chat_history: list[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
    return_trace: bool = False,
) -> str | Tuple[str, List[Dict[str, Any]]]:
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
    result_messages = result.get("messages", [])
    out = _final_ai_text(result_messages)
    logger.info("LangChain agent finished (output length=%d)", len(out))
    text = out or "(No assistant text returned.)"
    if return_trace:
        trace = _extract_tool_trace(result_messages)
        return text, trace
    return text


async def iter_tool_agent(
    user_input: str,
    *,
    chat_history: list[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
    trace_out: Optional[List[Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """Stream assistant tokens from the tool-calling agent.

    If ``trace_out`` is a list, it is replaced with :func:`_extract_tool_trace` output
    when the graph finishes (from the final ``on_chain_end`` payload when available).
    """
    from langchain.agents import create_agent

    llm = get_chat_model()
    tools = build_all_tools(allow_writes=allow_writes)
    verbose = settings.agent_langchain_verbose or logger.isEnabledFor(logging.DEBUG)

    graph = create_agent(
        llm,
        tools=tools,
        system_prompt=BOARD_MANAGER_SYSTEM + system_extra,
        debug=verbose,
    )
    messages: list[BaseMessage] = list(chat_history) + [HumanMessage(content=user_input)]

    async for event in graph.astream_events(
        {"messages": messages},
        version="v2",
        config={"recursion_limit": _recursion_limit()},
    ):
        kind = event.get("event")
        if trace_out is not None and kind == "on_chain_end":
            data = event.get("data") or {}
            out = data.get("output")
            if isinstance(out, dict):
                msgs = out.get("messages")
                if isinstance(msgs, list) and msgs:
                    trace_out[:] = _extract_tool_trace(msgs)[:120]
        if kind == "on_chat_model_stream":
            content = event.get("data", {}).get("chunk", {}).content
            if content:
                if isinstance(content, str):
                    yield content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            yield str(part.get("text", ""))
                        elif isinstance(part, str):
                            yield part
