"""Unit tests for LangChain message → tool trace extraction in boardman.agent.runner."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from boardman.agent.runner import (
    _extract_tool_trace,
    _normalize_trace_args,
    _tool_call_records,
)


def test_normalize_trace_args_parses_json_object_string():
    assert _normalize_trace_args('{"a": 1}') == {"a": 1}
    assert _normalize_trace_args("{}") == {}
    assert _normalize_trace_args("not json") == "not json"
    assert _normalize_trace_args({"x": 2}) == {"x": 2}


def test_tool_call_records_prefers_tool_calls_then_additional_kwargs():
    m1 = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "plaky_list_tasks", "args": {"status": "open"}}],
    )
    assert _tool_call_records(m1)[0]["name"] == "plaky_list_tasks"

    m2 = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "k1",
                    "function": {"name": "github_fetch_file", "arguments": '{"path": "README.md"}'},
                }
            ]
        },
    )
    recs = _tool_call_records(m2)
    assert recs[0].get("name") == "github_fetch_file"


def test_extract_tool_trace_pairs_tool_message_by_id():
    ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-1", "name": "plaky_get_task", "args": {"task_id": "42"}},
        ],
    )
    tm = ToolMessage(content=json.dumps({"ok": True}), tool_call_id="call-1", name="plaky_get_task")
    traces = _extract_tool_trace([HumanMessage(content="hi"), ai, tm])
    assert len(traces) == 1
    assert traces[0]["tool_name"] == "plaky_get_task"
    assert traces[0]["tool_call_id"] == "call-1"
    assert traces[0]["status"] == "ok"
    assert traces[0]["args"] == {"task_id": "42"}
    assert "ok" in (traces[0]["result_summary"] or "")


def test_extract_tool_trace_fills_name_from_tool_message_when_unknown():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "x", "name": "", "args": {}}],
    )
    tm = ToolMessage(content="done", tool_call_id="x", name="plaky_board_schema")
    traces = _extract_tool_trace([ai, tm])
    assert traces[0]["tool_name"] == "plaky_board_schema"


def test_extract_tool_trace_orphan_tool_message():
    traces = _extract_tool_trace(
        [ToolMessage(content="orphan", tool_call_id="", name="scan_local_repo")]
    )
    assert len(traces) == 1
    assert traces[0]["status"] == "ok"
    assert traces[0]["args"] is None


def test_extract_tool_trace_caps_length():
    msgs = []
    for i in range(130):
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[{"id": f"id{i}", "name": "thoughts", "args": {}}],
            )
        )
        msgs.append(ToolMessage(content="x", tool_call_id=f"id{i}", name="thoughts"))
    out = _extract_tool_trace(msgs)
    assert len(out) == 120
