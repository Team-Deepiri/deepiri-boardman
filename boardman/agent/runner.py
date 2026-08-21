"""LangChain tool-calling agent (optional; falls back if deps/model fail)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage

from boardman.agent.prompts import BOARD_MANAGER_SYSTEM
from boardman.agent.tool_timing import start_turn, turn_timing
from boardman.agent.tools import build_all_tools
from boardman.llm.factory import get_chat_model
from boardman.settings import settings

logger = logging.getLogger(__name__)


def _timed_tools(allow_writes: bool) -> list:
    """Process-level immutable tool registry, timing wrapper included.

    The cache lives in build_all_tools and is keyed on both variants. Holding a second
    cache of the wrapped copies here meant two layers memoising the same tools.
    """
    return build_all_tools(allow_writes=allow_writes, timed=True)


def _recursion_limit(allow_writes: bool = True) -> int:
    """PDF plan step 4: bounded loops. Reads get a tight ceiling (they should finish in
    a few tool rounds); writes get more headroom; the env override still wins."""
    n = int(getattr(settings, "agent_recursion_limit", 0) or 0)
    if n:
        return max(5, min(80, n))
    return 16 if allow_writes else 10


def _graph_config(allow_writes: bool) -> dict[str, Any]:
    """One config for every graph invocation, so the counters cannot be attached to some
    paths and not others."""
    from boardman.observability.langchain_counter import make_counting_callback

    cfg: dict[str, Any] = {"recursion_limit": _recursion_limit(allow_writes)}
    cb = make_counting_callback()
    if cb is not None:
        cfg["callbacks"] = [cb]
    return cfg


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


# "Let me fetch that now." shipped as a FINAL answer is a dead end for the user: the model
# announced a tool call that never landed. Detect that shape so we can force one more round.
_PREAMBLE_PATTERNS = (
    "let me fetch",
    "let me check",
    "let me look",
    "let me pull",
    "let me get",
    "i'll fetch",
    "i will fetch",
    "i'll check",
    "i will check",
    "i'll look",
    "here's what i'll do",
    "here is what i'll do",
    "fetching now",
    "one moment",
    "stand by",
)


def _looks_like_unfulfilled_preamble(text: str) -> bool:
    """True when the reply only PROMISES work instead of delivering it.

    Deliberately conservative: long or structured answers (headings, bullets, numbered
    findings) are never treated as preamble even if they contain a stray "let me check".
    """
    t = (text or "").strip()
    # Deliberately 600: a long answer may contain "let me check" in passing and
    # is already substantive. A bare promise is short by nature.
    if not t or len(t) > 600:
        return False
    low = t.lower()
    if not any(p in low for p in _PREAMBLE_PATTERNS):
        return False
    # Real answers carry structure or substance; a bare promise carries neither.
    structured = ("\n- " in t) or ("\n* " in t) or ("\n#" in t) or ("\n1." in t) or ("```" in t)
    return not structured


def _tool_call_records(m: AIMessage) -> list[dict[str, Any]]:
    tcalls: list[dict[str, Any]] = []
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


def _extract_tool_trace(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    index_by_id: dict[str, int] = {}
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
) -> str | tuple[str, list[dict[str, Any]]]:
    from langchain.agents import create_agent

    llm = get_chat_model()
    start_turn()
    tools = _timed_tools(allow_writes)
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
    cfg = _graph_config(allow_writes)
    result = await graph.ainvoke(
        {"messages": messages},
        config=cfg,
    )
    result_messages = result.get("messages", [])
    logger.info("agent turn tool time: %s", turn_timing())
    out = _final_ai_text(result_messages)

    if _looks_like_unfulfilled_preamble(out):
        # The turn ended on a promise ("Let me fetch this now.") instead of an answer.
        # Give it exactly one more round to deliver, using everything it already gathered.
        # Logged with the triggering text so false positives show up in production.
        from boardman.observability.counters import bump

        bump("agent.preamble_retry")
        logger.info(
            "agent returned an unfulfilled preamble (%d chars: %r); forcing one completion round",
            len(out),
            out[:120],
        )
        nudge = HumanMessage(
            content=(
                "You ended your turn by describing what you were going to do instead of "
                "answering. Produce the FINAL answer now from the tool results you already "
                "have. If a tool call is still required, make it now. Do not narrate intent."
            )
        )
        try:
            result = await graph.ainvoke(
                {"messages": list(result_messages) + [nudge]},
                config=cfg,
            )
            retry_messages = result.get("messages", [])
            retry_out = _final_ai_text(retry_messages)
            if retry_out and not _looks_like_unfulfilled_preamble(retry_out):
                logger.info("preamble retry delivered (%d chars, was %d)", len(retry_out), len(out))
                bump("agent.preamble_retry_helped")
                out, result_messages = retry_out, retry_messages
            else:
                logger.info("preamble retry did not improve the reply; keeping original")
                bump("agent.preamble_retry_no_help")
        except Exception as e:  # noqa: BLE001 — keep the original reply if the retry fails
            logger.warning("preamble completion round failed: %s", e)
            bump("agent.preamble_retry_failed")

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
    trace_out: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    """Stream assistant tokens from the tool-calling agent.

    If ``trace_out`` is a list, it is replaced with :func:`_extract_tool_trace` output
    when the graph finishes (from the final ``on_chain_end`` payload when available).
    """
    from langchain.agents import create_agent

    llm = get_chat_model()
    start_turn()
    tools = _timed_tools(allow_writes)
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
        config=_graph_config(allow_writes),
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
