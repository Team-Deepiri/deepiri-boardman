"""Intent-vs-reality engine: one case per conclusion value, rendering budget."""

from __future__ import annotations

from unittest.mock import patch

from boardman.cognition.behaviors import BEHAVIORS
from boardman.cognition.evidence import BehaviorSpec, Evidence
from boardman.cognition.intent_reality import compare_intent_to_reality, verdict_to_dict
from boardman.cognition.rendering import render_cognition_block


def _make_spec(expected: tuple[str, ...]) -> BehaviorSpec:
    return BehaviorSpec(
        behavior_key="test_behavior",
        description="test",
        expected_present=expected,
        evidence=(
            Evidence(
                kind="fact",
                subject="test",
                value="test evidence",
                source_type="code",
                source_ref="test.py:1",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    )


def test_aligned_when_all_present(tmp_path):
    """All expected items present produces ALIGNED."""
    (tmp_path / "boardman").mkdir()
    (tmp_path / "file_a.py").write_text("def my_func(): pass")
    (tmp_path / "file_b.py").write_text("content")
    spec = _make_spec(("file_a.py:my_func", "file_b.py"))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=tmp_path):
        v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED"
    assert v.confidence == "high"


def test_broken_when_all_absent(tmp_path):
    """All expected items absent produces BROKEN."""
    (tmp_path / "boardman").mkdir()
    spec = _make_spec(("nonexistent.py",))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=tmp_path):
        v = compare_intent_to_reality(spec)
    assert v.conclusion == "BROKEN"
    assert v.confidence == "high"


def test_partial_when_some_absent(tmp_path):
    """Some present and some absent produces PARTIAL."""
    (tmp_path / "boardman").mkdir()
    (tmp_path / "exists.py").write_text("content")
    spec = _make_spec(("exists.py", "missing.py"))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=tmp_path):
        v = compare_intent_to_reality(spec)
    assert v.conclusion == "PARTIAL"
    assert v.confidence == "high"


def test_unknown_when_no_root():
    """No repo root produces UNKNOWN, not BROKEN."""
    spec = _make_spec(("any.py",))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=None):
        v = compare_intent_to_reality(spec)
    assert v.conclusion == "UNKNOWN"
    assert v.confidence == "low"


def test_function_absent_is_broken(tmp_path):
    """A file that exists but does not contain the named function is BROKEN."""
    (tmp_path / "boardman").mkdir()
    (tmp_path / "module.py").write_text("def other_thing(): pass")
    spec = _make_spec(("module.py:target_function",))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=tmp_path):
        v = compare_intent_to_reality(spec)
    assert v.conclusion == "BROKEN"


def test_pr_edit_links_late_issue_reference_is_aligned():
    """The seeded behavior for Demo 5 evaluates to ALIGNED with real repo files."""
    spec = next(s for s in BEHAVIORS if s.behavior_key == "pr_edit_links_late_issue_reference")
    v = compare_intent_to_reality(spec)
    assert v.conclusion == "ALIGNED"


def test_verdict_to_dict_shape():
    spec = _make_spec(("any.py",))
    with patch("boardman.cognition.intent_reality._repo_root", return_value=None):
        v = compare_intent_to_reality(spec)
    d = verdict_to_dict(v)
    assert "behavior_key" in d
    assert "conclusion" in d
    assert "evidence" in d
    assert isinstance(d["evidence"], list)


def test_render_cognition_block_within_budget():
    """Rendering stays within its character budget."""
    cognition = {
        "cognition_state": "fresh",
        "verdicts": [
            {
                "behavior_key": f"b{i}",
                "conclusion": "BROKEN",
                "confidence": "high",
                "explanation": "missing",
                "evidence": [{"kind": "fact", "source_ref": "x.py", "value": "v"}],
            }
            for i in range(20)
        ],
        "contradictions": [
            {"entity": f"issue#{i}", "description": "drift found", "severity": "high"}
            for i in range(10)
        ],
    }
    result = render_cognition_block(cognition, max_chars=1500)
    assert len(result) <= 1500
    assert "BROKEN" in result
    assert "contradictions" in result.lower()


def test_render_cognition_block_empty_returns_empty():
    assert render_cognition_block(None) == ""
    assert render_cognition_block({}) == ""
    assert render_cognition_block({"cognition_state": "fresh"}) == ""
