"""Unit tests for PlakyClient field PATCH value heuristics."""

from boardman.plaky.client import PlakyClient


def test_patch_value_candidates_single_github_repo():
    cands = PlakyClient._patch_value_candidates("acme/widget")
    assert cands[0] == "acme/widget"
    assert ["acme/widget"] in cands
    assert {"tagValues": ["acme/widget"]} in cands


def test_patch_value_candidates_list_of_tag_option_ids():
    cands = PlakyClient._patch_value_candidates([42, 43])
    assert {"tagValues": [42, 43]} in cands
    assert {"tagValues": [{"id": 42}, {"id": 43}]} in cands


def test_patch_value_candidates_multi_github_repo_comma_joined():
    joined = "acme/foo, org/bar"
    cands = PlakyClient._patch_value_candidates(joined)
    assert cands[0] == joined
    assert ["acme/foo", "org/bar"] in cands
    assert {"tagValues": ["acme/foo", "org/bar"]} in cands
    assert [joined] in cands  # still try single string after list attempts
