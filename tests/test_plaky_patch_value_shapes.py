"""PlakyClient field PATCH value shapes (person + tag columns)."""

from __future__ import annotations

from boardman.plaky.client import PlakyClient


def test_person_id_candidates_prefer_users_shape_string_ids():
    cands = PlakyClient._patch_value_candidates("291493")
    assert cands[0] == {"users": [{"id": "291493"}], "teams": []}
    assert {"users": [{"id": 291493}], "teams": []} in cands
    assert {"assignedUsers": [{"id": 291493}], "assignedTeams": []} in cands
    assert "291493" in cands


def test_repo_string_candidates_include_tag_arrays():
    cands = PlakyClient._patch_value_candidates("Team-Deepiri/deepiri-platform")
    assert cands[0] == "Team-Deepiri/deepiri-platform"
    assert ["Team-Deepiri/deepiri-platform"] in cands
    assert {"tagValues": ["Team-Deepiri/deepiri-platform"]} in cands


def test_short_digit_string_not_treated_as_plaky_user_id():
    cands = PlakyClient._patch_value_candidates("0")
    assert cands == ["0"]


def test_status_label_string_not_wrapped_as_person_assignee():
    cands = PlakyClient._patch_value_candidates("in progress")
    assert cands == ["in progress"]
    assert not any(isinstance(x, dict) and "assignedUsers" in x for x in cands)
