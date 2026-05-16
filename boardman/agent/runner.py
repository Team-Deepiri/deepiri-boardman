"""LangChain tool-calling agent (optional; falls back if deps/model fail)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage

from boardman.agent.ollama_text_tool_coercion import (
    CoerceOllamaTextToolCallsMiddleware,
    _allowed_tool_names,
    strip_disallowed_tool_json_blobs,
)
from boardman.agent.prompts import BOARD_MANAGER_SYSTEM
from boardman.agent.tools import build_all_tools
from boardman.llm.factory import get_chat_model
from boardman.settings import settings

logger = logging.getLogger(__name__)

# Local models often emit tool JSON in assistant text; LangGraph only runs tools when
# AIMessage.tool_calls is set (see langchain.agents.factory _make_model_to_tools_edge).
_OLLAMA_TEXT_TOOL_COERCION = (CoerceOllamaTextToolCallsMiddleware(),)

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


def _brief_tool_fallback(messages: list[AnyMessage]) -> str:
    """When the model returns no usable assistant text after tools, surface something readable."""
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            continue
        raw = str(m.content or "").strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw[:500] + ("…" if len(raw) > 500 else "")
            return (
                "The model did not return a normal summary (long sessions or context limits can cause this). "
                f"Latest tool output (truncated):\n```\n{snippet}\n```"
            )
        if isinstance(d, dict) and isinstance(d.get("tasks"), list):
            n = len(d["tasks"])
            return (
                f"There are **{n}** matching Plaky task(s) in the tool result. "
                "The model did not summarize them; try again or start a **new session** if replies look truncated."
            )
        if isinstance(d, dict) and d.get("ok") is False:
            msg = str(d.get("message") or d.get("error") or "error")
            return f"A tool reported an issue: {msg}"
        snippet = raw[:500] + ("…" if len(raw) > 500 else "")
        return (
            "The model did not add a summary after running tools. "
            f"Latest tool output (truncated):\n```\n{snippet}\n```"
        )
    return ""


def _final_ai_text(
    messages: list[AnyMessage],
    *,
    allowed_tool_names: set[str] | None = None,
) -> str:
    """
    Return visible assistant text from the graph's message list.

    After tool calls, LangChain typically appends a second AIMessage with the real reply.
    Taking the *first* AIMessage when scanning reversed() wrongly returns the tool-call JSON
    or markdown the model typed before tools ran — use the **last** non-empty AIMessage text.
    """
    last = ""
    for m in messages:
        if isinstance(m, AIMessage):
            text = _message_content_to_text(m.content)
            if allowed_tool_names:
                text = strip_disallowed_tool_json_blobs(text, allowed_tool_names)
            if text:
                last = text
    if last.strip():
        return last
    fb = _brief_tool_fallback(messages)
    return fb if fb.strip() else last


def _extract_messages_from_chain_end_event(event: dict[str, Any]) -> list[AnyMessage] | None:
    """Best-effort parse of LangGraph / LangChain `on_chain_end` output."""
    data = event.get("data") or {}
    out = data.get("output")
    if isinstance(out, dict):
        msgs = out.get("messages")
        if isinstance(msgs, list):
            return msgs  # type: ignore[return-value]
    # Some LangGraph builds emit (state_dict, metadata) tuples.
    if isinstance(out, tuple) and out:
        head = out[0]
        if isinstance(head, dict):
            msgs = head.get("messages")
            if isinstance(msgs, list):
                return msgs  # type: ignore[return-value]
    return None


async def run_tool_agent(
    user_input: str,
    *,
    chat_history: list[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
    request_model: str | None = None,
) -> str:
    from langchain.agents import create_agent

    llm = get_chat_model(request_model=request_model)
    tools = build_all_tools(allow_writes=allow_writes)
    allowed = _allowed_tool_names(list(tools))
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
        middleware=_OLLAMA_TEXT_TOOL_COERCION,
        debug=verbose,
    )
    messages: list[BaseMessage] = list(chat_history) + [HumanMessage(content=user_input)]
    result = await graph.ainvoke(
        {"messages": messages},
        config={"recursion_limit": _recursion_limit()},
    )
    out = _final_ai_text(result.get("messages", []), allowed_tool_names=allowed)
    logger.info("LangChain agent finished (output length=%d)", len(out))
    return out or "(No assistant text returned.)"


async def iter_tool_agent(
    user_input: str,
    *,
    chat_history: list[BaseMessage],
    allow_writes: bool,
    system_extra: str = "",
    request_model: str | None = None,
) -> AsyncIterator[str]:
    """Stream assistant tokens from the tool-calling agent."""
    from langchain.agents import create_agent

    llm = get_chat_model(request_model=request_model)
    tools = build_all_tools(allow_writes=allow_writes)
    allowed = _allowed_tool_names(list(tools))
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
        middleware=_OLLAMA_TEXT_TOOL_COERCION,
        debug=verbose,
    )
    messages: list[BaseMessage] = list(chat_history) + [HumanMessage(content=user_input)]

    streamed_chunks: list[str] = []
    final_messages: list[AnyMessage] | None = None

    # We use astream_events to catch 'on_chat_model_stream' events.
    # The 'agent' node in create_agent uses the LLM to decide on next steps or final answer.
    async for event in graph.astream_events(
        {"messages": messages},
        version="v2",
        config={"recursion_limit": _recursion_limit()},
    ):
        kind = event.get("event")
        if kind == "on_chain_end":
            msgs = _extract_messages_from_chain_end_event(event)
            if msgs:
                final_messages = msgs

        # Ollama often streams tool-shaped JSON + stray prose; middleware coerces tools after
        # each full model turn, so streaming those tokens produces a confusing UI. Accumulate
        # only for divergence logging; emit the final assistant text once below.
        if kind == "on_chat_model_stream":
            content = event.get("data", {}).get("chunk", {}).content
            if content:
                if isinstance(content, str):
                    streamed_chunks.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            streamed_chunks.append(str(part.get("text", "")))
                        elif isinstance(part, str):
                            streamed_chunks.append(part)

    # Ollama/LangChain sometimes stream only the first model turn (tool JSON); the final
    # answer may not emit chat_model_stream tokens. Patch from authoritative graph messages.
    streamed_text = "".join(streamed_chunks)
    if final_messages:
        full_text = _final_ai_text(final_messages, allowed_tool_names=allowed)
        if not full_text.strip():
            logger.warning(
                "iter_tool_agent: model returned empty final text (session may look stuck); emitting fallback"
            )
            yield (
                "I did not get a text reply from the model after running tools. "
                "Try sending the question again, or start a **new session** if the chat is very long."
            )
            return
        # Stream chunks are not forwarded to the client (we only accumulate for comparison), so if
        # streamed text already equals the final graph text we must still emit once — otherwise
        # the SSE layer yields zero tokens and follow-up turns look "dead".
        if streamed_text.strip() == full_text.strip():
            yield full_text
            return
        if streamed_text and full_text.startswith(streamed_text):
            suffix = full_text[len(streamed_text) :]
            if suffix.strip():
                logger.info(
                    "iter_tool_agent: appending %d chars missed by stream (prefix match)",
                    len(suffix),
                )
                yield suffix
            return
        if not streamed_text.strip():
            yield full_text
            return
        logger.info(
            "iter_tool_agent: streaming diverged from final AI text; emitting full reply (%d chars)",
            len(full_text),
        )
        yield "\n\n" + full_text
