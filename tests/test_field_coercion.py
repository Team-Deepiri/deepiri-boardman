"""Near-miss field values resolve; unrecognized ones must still be refused.

"Make me 5 Plaky tasks" failed because the assistant's create path rejected `Feature` on a
board whose Type options are Story/Task/Bug/Research — a word the GitHub automation maps
happily. The model then had to guess why, guessed wrong ("Plaky needs text, not numbers"),
and asked the user to choose instead of retrying.

Coercing too eagerly is the worse failure though: a status nobody recognises must not
quietly become "In Progress", because that writes a wrong state onto the board with full
confidence.
"""

from __future__ import annotations

from boardman.plaky.field_coercion import coerce_field_values

BOARD = {
    "fields": [
        {
            "key": "status-5",
            "name": "Type",
            "type": "STATUS",
            "options": [
                {"id": "0", "name": "Story"},
                {"id": "9", "name": "Task"},
                {"id": "10", "name": "Bug"},
                {"id": "12", "name": "Research"},
            ],
        },
        {
            "key": "status-6",
            "name": "Status",
            "type": "STATUS",
            "options": [
                {"id": "0", "name": "NEEDS ASSIGNED"},
                {"id": "2", "name": "In Progress"},
                {"id": "1", "name": "Completed"},
            ],
        },
        {
            "key": "status-7",
            "name": "Priority",
            "type": "STATUS",
            "options": [
                {"id": "0", "name": "VERY IMPORTANT"},
                {"id": "1", "name": "High"},
                {"id": "2", "name": "Medium"},
            ],
        },
        {"key": "person-3", "name": "Assignee", "type": "PERSON"},
    ]
}


def test_exact_label_passes_through() -> None:
    out, notes = coerce_field_values({"status-6": "In Progress"}, BOARD)
    assert out == {"status-6": "In Progress"}
    assert notes == []


def test_option_id_becomes_its_label() -> None:
    """The model may echo an id it saw in the schema. Validation matches on labels, so an
    id has to come back out as one or a correct value looks invalid."""
    out, _ = coerce_field_values({"status-6": 0, "status-7": "1"}, BOARD)
    assert out == {"status-6": "NEEDS ASSIGNED", "status-7": "High"}


def test_case_is_ignored() -> None:
    out, _ = coerce_field_values({"status-6": "needs assigned"}, BOARD)
    assert out == {"status-6": "NEEDS ASSIGNED"}


def test_feature_resolves_the_way_the_automation_resolves_it() -> None:
    out, notes = coerce_field_values({"status-5": "Feature"}, BOARD)
    assert out == {"status-5": "Story"}
    assert notes and "no 'Feature' option" in notes[0] and "Story" in notes[0]


def test_priority_synonym_resolves() -> None:
    out, notes = coerce_field_values({"status-7": "Urgent"}, BOARD)
    assert out == {"status-7": "High"}
    assert notes


def test_unrecognized_status_is_left_for_the_validator_to_refuse() -> None:
    """The ladder defaults an unknown word to "In Progress". Applying that default here
    would put a state on the board that the user never asked for and cannot see is wrong."""
    out, notes = coerce_field_values({"status-6": "Backlog"}, BOARD)
    assert out == {"status-6": "Backlog"}
    assert notes == []

    out, _ = coerce_field_values({"status-6": "banana"}, BOARD)
    assert out == {"status-6": "banana"}


def test_non_option_fields_are_untouched() -> None:
    out, notes = coerce_field_values({"person-3": ["481106"], "text-1": "hello"}, BOARD)
    assert out == {"person-3": ["481106"], "text-1": "hello"}
    assert notes == []


def test_missing_schema_is_a_no_op() -> None:
    values = {"status-6": "In Progress"}
    out, notes = coerce_field_values(values, None)
    assert out == values
    assert notes == []


# --- repo context must stay parseable ------------------------------------------------


def test_planning_context_stays_valid_json_when_oversized() -> None:
    """It used to slice the SERIALIZED json at 24k, cutting mid-string: the payload stopped
    being valid JSON and the dropped sections left no trace, so a partial read looked
    complete. Trim fields, mark the cuts, keep the envelope parseable."""
    import json

    from boardman.agent.tools.github_tools import _budget_json

    payload = _budget_json(
        {
            "ok": True,
            "repo": "o/r",
            "structure": {"language": "Python"},
            "DIRECTION_md": "D" * 60000,
            "readme_md": "R" * 60000,
            "recent_commits_markdown": "C" * 20000,
        }
    )
    data = json.loads(payload)  # would raise on the old path
    assert data["ok"] is True and data["structure"] == {"language": "Python"}
    assert "DIRECTION_md" in data["truncated_fields"]
    assert len(payload) <= 24000


def test_small_context_is_not_marked_truncated() -> None:
    import json

    from boardman.agent.tools.github_tools import _budget_json

    data = json.loads(_budget_json({"ok": True, "repo": "o/r", "readme_md": "short"}))
    assert "truncated_fields" not in data
