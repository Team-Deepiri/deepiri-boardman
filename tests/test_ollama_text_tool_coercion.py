from __future__ import annotations

import json

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from boardman.agent.ollama_text_tool_coercion import (
    CoerceOllamaTextToolCallsMiddleware,
    _extract_all_textual_tool_calls,
    _parse_toolish_dict,
    _tool_name_and_args,
    strip_disallowed_tool_json_blobs,
)


def _dummy_list_tasks(status: str = "open") -> str:
    return "ok"


@pytest.mark.asyncio
async def test_middleware_coerces_json_content_to_tool_calls() -> None:
    tool = StructuredTool.from_function(
        _dummy_list_tasks,
        name="plaky_list_tasks",
        description="list",
    )
    mw = CoerceOllamaTextToolCallsMiddleware()

    payload = json.dumps({"name": "plaky_list_tasks", "arguments": {"status": "open"}})

    async def handler(request: ModelRequest) -> ModelResponse:
        assert request.tools
        return ModelResponse(result=[AIMessage(content=payload)])

    req = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage("x")],
        system_message=SystemMessage("sys"),
        tools=[tool],
    )

    out = await mw.awrap_model_call(req, handler)
    assert len(out.result) == 1
    msg = out.result[0]
    assert isinstance(msg, AIMessage)
    assert msg.tool_calls
    assert msg.tool_calls[0]["name"] == "plaky_list_tasks"
    assert msg.tool_calls[0]["args"] == {"status": "open"}
    assert msg.content == ""


def test_parse_tool_name_arguments() -> None:
    raw = '{"name": "plaky_list_tasks", "arguments": {"status": "in_progress"}}'
    obj = _parse_toolish_dict(raw)
    assert obj is not None
    p = _tool_name_and_args(obj)
    assert p == ("plaky_list_tasks", {"status": "in_progress"})


def test_parse_markdown_fenced_json() -> None:
    raw = '```json\n{"name": "plaky_list_tasks", "arguments": {}}\n```'
    obj = _parse_toolish_dict(raw)
    assert obj is not None
    assert _tool_name_and_args(obj) == ("plaky_list_tasks", {})


@pytest.mark.asyncio
async def test_middleware_coerces_two_json_tool_blobs_in_one_message() -> None:
    t1 = StructuredTool.from_function(_dummy_list_tasks, name="plaky_list_tasks", description="a")
    t2 = StructuredTool.from_function(lambda: "ok", name="plaky_list_boards", description="b")
    allowed = {"plaky_list_tasks", "plaky_list_boards"}
    payload = (
        '{"name": "plaky_list_tasks", "arguments": {"status": "open"}}\n'
        'Some prose\n'
        '{"name": "plaky_list_boards", "arguments": {}}'
    )
    tcs = _extract_all_textual_tool_calls(payload, allowed)
    assert len(tcs) == 2
    assert tcs[0]["name"] == "plaky_list_tasks"
    assert tcs[1]["name"] == "plaky_list_boards"

    mw = CoerceOllamaTextToolCallsMiddleware()

    async def handler(request: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content=payload)])

    req = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage("x")],
        system_message=SystemMessage("sys"),
        tools=[t1, t2],
    )
    out = await mw.awrap_model_call(req, handler)
    msg = out.result[0]
    assert isinstance(msg, AIMessage)
    assert len(msg.tool_calls) == 2


def test_strip_disallowed_tool_json_removes_hallucinated_name() -> None:
    allowed = {"plaky_list_tasks"}
    raw = '```json\n{"name": "planning", "arguments": {}}\n```'
    assert strip_disallowed_tool_json_blobs(raw, allowed) == ""


@pytest.mark.asyncio
async def test_middleware_strips_hallucinated_tool_json_from_content() -> None:
    tool = StructuredTool.from_function(_dummy_list_tasks, name="plaky_list_tasks", description="list")
    mw = CoerceOllamaTextToolCallsMiddleware()
    bad = '```json\n{"name": "planning", "arguments": {}}\n```'

    async def handler(request: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content=bad)])

    req = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage("x")],
        system_message=SystemMessage("sys"),
        tools=[tool],
    )
    out = await mw.awrap_model_call(req, handler)
    msg = out.result[0]
    assert isinstance(msg, AIMessage)
    assert not msg.tool_calls
    assert str(msg.content or "").strip() == ""

