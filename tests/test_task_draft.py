"""Session task draft merge (Plaky field defaults)."""

from boardman.agent.task_draft import merge_draft_into_field_values


def test_merge_draft_tool_overrides():
    draft = {"field_values": {"a": "1", "b": "2"}, "summary": ""}
    out = merge_draft_into_field_values(draft, {"b": "9", "c": "3"})
    assert out == {"a": "1", "b": "9", "c": "3"}
