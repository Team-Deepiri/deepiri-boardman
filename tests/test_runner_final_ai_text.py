"""Regression: final assistant text must be the post-tool reply, not tool-call JSON."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from boardman.agent.runner import _final_ai_text


def test_final_ai_text_prefers_last_ai_message_after_tools():
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content='{"name": "plaky_list_tasks", "arguments": {}}'),
        ToolMessage(content='{"tasks":[]}', tool_call_id="call_1"),
        AIMessage(content="Here are the open tasks:\n- **A**"),
    ]
    assert _final_ai_text(msgs) == "Here are the open tasks:\n- **A**"


def test_final_ai_text_single_turn():
    assert _final_ai_text([AIMessage(content="Hello.")]) == "Hello."


def test_final_ai_text_strips_fake_tool_then_uses_tool_fallback():
    allowed = {"plaky_list_tasks"}
    msgs = [
        HumanMessage(content="list tasks"),
        ToolMessage(
            content=json.dumps({"ok": True, "tasks": [{"id": "a"}, {"id": "b"}]}),
            tool_call_id="c1",
        ),
        AIMessage(content='```json\n{"name": "planning", "arguments": {}}\n```'),
    ]
    out = _final_ai_text(msgs, allowed_tool_names=allowed)
    assert "2" in out
    assert "matching Plaky task" in out
