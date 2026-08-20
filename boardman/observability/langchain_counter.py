"""Count what the tool-calling agent actually sends to the model.

The LangChain path does not go through ``chat_complete``, so the counters there see none
of it — and the tool-calling path is the one every real chat turn takes. A callback is the
only place that sees every model round trip inside the graph, including the extra rounds
the agent takes after each tool result.

Cheap by construction: two integer bumps and a length sum per model call, and the handler
is only ever attached to the agent graph, never to a hot loop.
"""

from __future__ import annotations

from typing import Any


def _prompt_chars(prompts: Any, kwargs: dict[str, Any]) -> int:
    """Characters sent on this round, from whichever shape LangChain hands us."""
    total = 0
    for p in prompts or []:
        total += len(str(p))
    if total:
        return total
    for msgs in kwargs.get("messages") or []:
        for m in msgs or []:
            total += len(str(getattr(m, "content", "") or ""))
    return total


def make_counting_callback() -> Any:
    """A LangChain callback handler that records model calls, or None if unavailable."""
    try:
        from langchain_core.callbacks.base import AsyncCallbackHandler
    except Exception:  # pragma: no cover - langchain always present in this service
        return None

    from boardman.observability.counters import bump, observe

    class _Counter(AsyncCallbackHandler):
        def __init__(self) -> None:
            super().__init__()
            # A fresh handler per graph invocation, so this flag is per TURN.
            self._recorded_first = False

        async def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
            bump("llm.calls")
            bump("llm.calls.agent")
            chars = 0
            for batch in messages or []:
                for m in batch or []:
                    chars += len(str(getattr(m, "content", "") or ""))
            if chars:
                observe("llm.prompt_chars", chars)
                # The turn's FIRST model call carries the system prompt; later rounds carry
                # it PLUS every tool result so far. Recording the last one made the gauge a
                # measure of how much a tool returned, and comparing two turns that made
                # different numbers of tool calls compared nothing at all.
                if not self._recorded_first:
                    self._recorded_first = True
                    from boardman.observability.counters import set_gauge

                    set_gauge("llm.last_prompt_chars", chars)

        async def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
            # Text-completion models never reach on_chat_model_start.
            bump("llm.calls")
            bump("llm.calls.agent")
            chars = _prompt_chars(prompts, kwargs)
            if chars:
                observe("llm.prompt_chars", chars)

        async def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
            bump("tool.calls")
            name = ""
            if isinstance(serialized, dict):
                name = str(serialized.get("name") or "")
            if name:
                bump(f"tool.calls.{name}")

    return _Counter()
