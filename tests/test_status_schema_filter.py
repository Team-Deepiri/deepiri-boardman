"""Board schema → status option matching for plaky_list_tasks."""

from __future__ import annotations

from boardman.agent.tools import plaky_tools as pt
from boardman.plaky.board_schema import resolve_status_field_option_values


def test_resolve_status_maps_in_progress_to_option_ids() -> None:
    normalized = {
        "fields": [
            {
                "name": "Status",
                "type": "STATUS",
                "key": "status-field-1",
                "options": [
                    {"name": "In Progress", "id": "uuid-in-prog"},
                    {"name": "Done", "id": "uuid-done"},
                ],
            }
        ]
    }
    fk, acc = resolve_status_field_option_values(normalized, "in_progress")
    assert fk == "status-field-1"
    assert "uuid-in-prog" in acc

    item = {"fields": {"status-field-1": "uuid-in-prog"}}
    assert pt._item_matches_schema_status(item, fk, acc) is True

    item2 = {"itemFields": [{"fieldKey": "status-field-1", "value": {"id": "uuid-in-prog"}}]}
    assert pt._item_matches_schema_status(item2, fk, acc) is True


def test_resolve_status_in_progress_excludes_revisions_in_progress() -> None:
    """Query 'in progress' must not pick the distinct 'Revisions In Progress' status option."""
    normalized = {
        "fields": [
            {
                "name": "Status",
                "type": "STATUS",
                "key": "status-1",
                "options": [
                    {"name": "In Progress", "id": "opt-ip"},
                    {"name": "Revisions In Progress", "id": "opt-rip"},
                    {"name": "Done", "id": "opt-done"},
                ],
            }
        ]
    }
    fk, acc = resolve_status_field_option_values(normalized, "in_progress")
    assert fk == "status-1"
    assert "opt-ip" in acc
    assert "opt-rip" not in acc

    fk2, acc2 = resolve_status_field_option_values(normalized, "revisions in progress")
    assert fk2 == "status-1"
    assert "opt-rip" in acc2
    assert "opt-ip" not in acc2


def test_resolve_status_no_match_falls_back_to_empty_set() -> None:
    fk, acc = resolve_status_field_option_values({"fields": []}, "in_progress")
    assert fk is None
    assert acc == set()


def test_resolve_status_plaky_configuration_values_shape() -> None:
    """Plaky v1/public board fields expose options under configuration.values (key + title)."""
    normalized = {
        "fields": [
            {
                "name": "Status",
                "type": "STATUS",
                "key": "status-1",
                "configuration": {
                    "values": [
                        {"key": "4", "title": "Paused"},
                        {"key": "5", "title": "In Progress"},
                    ]
                },
            }
        ]
    }
    fk, acc = resolve_status_field_option_values(normalized, "in_progress")
    assert fk == "status-1"
    assert "5" in acc
    assert "in progress" in acc

    item = {
        "fields": [
            {"key": "status-1", "type": "STATUS", "title": "Status", "value": "5"},
        ]
    }
    assert pt._item_matches_schema_status(item, fk, acc) is True


def test_resolve_status_fallback_non_status_column_name() -> None:
    """Plaky boards may label the column 'Workflow' instead of 'Status'."""
    normalized = {
        "fields": [
            {
                "name": "Workflow",
                "type": "SINGLE_SELECT",
                "key": "wf-1",
                "options": [{"name": "In Progress", "id": "opt-wf-ip"}],
            }
        ]
    }
    fk, acc = resolve_status_field_option_values(normalized, "in_progress")
    assert fk == "wf-1"
    assert "opt-wf-ip" in acc
