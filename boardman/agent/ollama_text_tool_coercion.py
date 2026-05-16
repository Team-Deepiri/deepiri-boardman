"""Repair Ollama-style tool JSON emitted as plain assistant text.

LangChain's ``create_agent`` only routes to the tool node when ``AIMessage.tool_calls``
is non-empty. Some local models (Llama 3, Qwen via Ollama) put a JSON object with
``name`` / ``arguments`` in ``content`` instead of using native ``tool_calls``; the
graph then hits END immediately and the user sees raw JSON.

This middleware copies such payloads into structured ``tool_calls`` after each model
turn so ``ToolNode`` runs normally.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.tool import tool_call
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    t = _FENCE_RE.sub("", t)
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3].rstrip()
    return t.strip()


def _first_balanced_brace_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_toolish_dict(content: str) -> dict[str, Any] | None:
    raw = _strip_markdown_fences(content)
    blob = _first_balanced_brace_object(raw) or raw
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_name_and_args(obj: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if "name" in obj and "arguments" in obj:
        name = obj["name"]
        args = obj["arguments"]
        if isinstance(name, str) and isinstance(args, dict):
            return name, args
        if isinstance(name, str) and isinstance(args, str):
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return name, parsed
        return None
    fn = obj.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        args = fn.get("arguments")
        if not isinstance(name, str):
            return None
        if isinstance(args, dict):
            return name, args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return name, parsed
    return None


def _allowed_tool_names(tools: list[BaseTool | dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tools:
        if isinstance(t, BaseTool):
            names.add(t.name)
        elif isinstance(t, dict):
            fn = t.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                names.add(fn["name"])
    return names


def strip_disallowed_tool_json_blobs(text: str, allowed: set[str]) -> str:
    """
    Remove ``{ "name": "...", "arguments": ... }`` segments whose ``name`` is not a registered tool.

    Local models sometimes emit a fake tool call (e.g. ``planning``) as plain text after real tools
    ran; LangChain does not execute it, but the JSON would otherwise be shown to the user.
    """
    out: list[str] = []
    work = text
    for _ in range(96):
        if not work:
            break
        i = work.find("{")
        if i < 0:
            out.append(work)
            break
        out.append(work[:i])
        seg = work[i:]
        blob = _first_balanced_brace_object(seg)
        if not blob:
            out.append(work[i])
            work = work[i + 1 :]
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            out.append(work[i])
            work = work[i + 1 :]
            continue
        parsed = _tool_name_and_args(obj) if isinstance(obj, dict) else None
        if parsed and parsed[0] not in allowed:
            work = seg[len(blob) :]
            continue
        out.append(blob)
        work = seg[len(blob) :]
    t = "".join(out)
    for _ in range(4):
        prev = t
        t = re.sub(r"^\s*```(?:json)?\s*\n?", "", t, count=1, flags=re.IGNORECASE)
        t = re.sub(r"\n?\s*```\s*$", "", t, count=1)
        t = t.strip()
        if t == prev:
            break
    return t


def _extract_all_textual_tool_calls(text: str, allowed: set[str]) -> list[dict[str, Any]]:
    """Scan ``text`` for one or more ``{ "name": ..., "arguments": ... }`` blobs (Ollama-style)."""
    collected: list[dict[str, Any]] = []
    work = text
    for _ in range(48):
        if "{" not in work:
            break
        start = work.find("{")
        segment = work[start:]
        blob = _first_balanced_brace_object(segment)
        if not blob:
            work = work[start + 1 :]
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            work = work[start + 1 :]
            continue
        if isinstance(obj, dict):
            parsed = _tool_name_and_args(obj)
            if parsed and parsed[0] in allowed:
                name, args = parsed
                collected.append(tool_call(id=str(uuid.uuid4()), name=name, args=args))
        work = work[start + len(blob) :]
    return collected


def _coerce_ai_message(msg: AIMessage, allowed: set[str]) -> AIMessage:
    if msg.tool_calls:
        return msg
    text = msg.text
    if not text or "{" not in text:
        return msg
    tcs = _extract_all_textual_tool_calls(text, allowed)
    if not tcs:
        stripped = strip_disallowed_tool_json_blobs(text, allowed)
        if stripped != text:
            return msg.model_copy(update={"content": stripped})
        return msg
    for tc in tcs:
        logger.info(
            "Coerced textual tool call into AIMessage.tool_calls (tool=%s, arg_keys=%s)",
            tc["name"],
            sorted((tc.get("args") or {}).keys()),
        )
    return msg.model_copy(update={"tool_calls": tcs, "content": ""})


def _coerce_result_messages(messages: list[BaseMessage], allowed: set[str]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, AIMessage):
            out.append(_coerce_ai_message(m, allowed))
        else:
            out.append(m)
    return out


class CoerceOllamaTextToolCallsMiddleware(AgentMiddleware):
    """``awrap_model_call`` hook used by ``create_agent`` (async agent path)."""

    tools: Sequence[BaseTool] = ()

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        allowed = _allowed_tool_names(list(request.tools))
        if not allowed:
            return response
        new_result = _coerce_result_messages(list(response.result), allowed)
        if new_result == response.result:
            return response
        return ModelResponse(result=new_result, structured_response=response.structured_response)
