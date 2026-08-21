"""The Type fallback ladder, pinned.

Boards disagree about their Type vocabulary: one has "Feature", another only "Task", few
have "Refactoring". `type_field_patch_candidates` decides which of a board's real options
best means the same thing, and that ordering is a team decision rather than something the
schema can tell us. Pinning it here is step 4 of the update process documented next to
_TYPE_FALLBACKS in boardman/plaky/task_tag_vocab.py (Sorge review, PR #88).
"""

from __future__ import annotations

import pytest

from boardman.plaky.task_tag_vocab import (
    TASK_TYPE_TAGS,
    priority_field_patch_candidates,
    status_field_patch_candidates,
    type_field_patch_candidates,
)


def test_the_canonical_type_is_always_tried_first() -> None:
    """A board that HAS the option must never be given a fallback instead."""
    for tag in TASK_TYPE_TAGS:
        assert type_field_patch_candidates(tag)[0] == tag


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        # Board 269031 has no "Feature" option; Story is the nearest thing it does have.
        ("Feature", ("Feature", "story", "task", "enhancement")),
        ("Story", ("Story", "feature", "task")),
        ("Bug", ("Bug", "issue", "defect", "fix")),
        ("Refactoring", ("Refactoring", "refactor", "chore", "task", "story")),
        ("Research", ("Research", "spike", "investigation", "story", "task")),
        # The agreed board vocabulary is Feature/Bug/Refactor/Research/Story, so these
        # reach Story before Task rather than the other way round.
        ("Documentation", ("Documentation", "docs", "story", "chore", "task")),
        ("Chore", ("Chore", "refactor", "story", "task")),
    ],
)
def test_the_ladder_order_is_what_the_team_agreed(
    canonical: str, expected: tuple[str, ...]
) -> None:
    assert type_field_patch_candidates(canonical) == expected


def test_feature_and_story_degrade_into_each_other() -> None:
    """Symmetry rule: if A falls back to B, B falls back to A."""
    assert "story" in [c.casefold() for c in type_field_patch_candidates("Feature")]
    assert "feature" in [c.casefold() for c in type_field_patch_candidates("Story")]


def test_an_unknown_type_gets_no_invented_fallbacks() -> None:
    """Leaving Type unset beats filing a Bug as a Story."""
    assert type_field_patch_candidates("Wombat") == ("Wombat",)
    assert type_field_patch_candidates("") == ()


def test_candidates_are_deduplicated_case_insensitively() -> None:
    for canonical in TASK_TYPE_TAGS:
        cands = [c.casefold() for c in type_field_patch_candidates(canonical)]
        assert len(cands) == len(set(cands))


def test_status_and_priority_ladders_also_lead_with_the_canonical_value() -> None:
    assert status_field_patch_candidates("In Progress")[0] == "In Progress"
    assert "wip" in status_field_patch_candidates("In Progress")
    assert priority_field_patch_candidates("Very Important")[0] == "Very Important"
    assert "critical" in priority_field_patch_candidates("Very Important")
