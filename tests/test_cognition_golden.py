"""Golden-answer tests: facts that must never silently drift.

Pins the verdict wiring so that removing a critical implementation flips the verdict.
This tests the cognition engine's wiring, not the linking behavior itself --
tests/test_pr_edited_relink.py is the regression guard for the actual behavior.
"""

from __future__ import annotations

from boardman.cognition.behaviors import BEHAVIORS
from boardman.cognition.intent_reality import compare_intent_to_reality


def test_pr_edit_links_late_issue_reference_is_aligned():
    """The Demo 5 behavior evaluates to ALIGNED: the implementation and test exist."""
    spec = next(s for s in BEHAVIORS if s.behavior_key == "pr_edit_links_late_issue_reference")
    v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED", (
        f"Expected ALIGNED but got {v.conclusion}: {v.explanation}. "
        "If this fails, reconcile_pr_issue_links or its test was removed."
    )


def test_all_seeded_behaviors_resolve():
    """Every registered behavior produces a verdict (never raises)."""
    for spec in BEHAVIORS:
        v = compare_intent_to_reality(spec)
        assert v.conclusion in ("ALIGNED", "PARTIAL", "BROKEN", "UNKNOWN")
        assert v.confidence in ("high", "low")


def test_pr_author_becomes_developer_is_aligned():
    spec = next(s for s in BEHAVIORS if s.behavior_key == "pr_author_becomes_developer")
    v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED"


def test_qa_assigned_at_pr_ready_is_aligned():
    spec = next(s for s in BEHAVIORS if s.behavior_key == "qa_assigned_at_pr_ready")
    v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED"


def test_reconcile_repairs_drift_is_aligned():
    spec = next(s for s in BEHAVIORS if s.behavior_key == "reconcile_repairs_drift")
    v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED"
