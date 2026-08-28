"""Cognition planning: dedup, ranking, and candidate validation."""

from __future__ import annotations

from boardman.cognition.planning import (
    PlannedTask,
    dedupe_against_existing_work,
    planned_task_from_dict,
    planned_task_to_dict,
    rank_candidates,
)


def _task(
    title: str, evidence: list[str] | None = None, sources: list[str] | None = None
) -> PlannedTask:
    return PlannedTask(
        title=title,
        problem=f"Problem for {title}",
        evidence=evidence or ["some evidence"],
        sources=sources or ["code"],
        acceptance_criteria=["it works"],
        verification_method="test",
    )


def test_candidate_matching_open_issue_is_dropped():
    """A candidate whose title matches an open issue is removed."""
    candidates = [_task("Add retry budget to plaky client")]
    open_issues = [{"title": "Add retry budget to plaky client", "number": 42}]
    result = dedupe_against_existing_work(candidates, open_issues, [], [])
    assert len(result) == 0


def test_candidate_not_matching_passes_through():
    """A candidate with no match in existing work survives."""
    candidates = [_task("Implement cognition engine")]
    open_issues = [{"title": "Fix login bug", "number": 1}]
    result = dedupe_against_existing_work(candidates, open_issues, [], [])
    assert len(result) == 1
    assert result[0].title == "Implement cognition engine"


def test_dedup_against_plaky_tasks():
    """A candidate matching a Plaky task title is dropped."""
    candidates = [_task("Add caching layer")]
    plaky_tasks = [{"name": "Add caching layer", "id": "t1"}]
    result = dedupe_against_existing_work(candidates, [], [], plaky_tasks)
    assert len(result) == 0


def test_dedup_is_case_insensitive():
    candidates = [_task("fix login bug")]
    open_issues = [{"title": "Fix Login Bug", "number": 1}]
    result = dedupe_against_existing_work(candidates, open_issues, [], [])
    assert len(result) == 0


def test_ranking_is_stable():
    """Same inputs always produce the same order."""
    candidates = [
        _task("A", evidence=["e1", "e2", "e3"], sources=["code", "test", "doc"]),
        _task("B", evidence=["e1"], sources=["code"]),
        _task("C", evidence=["e1", "e2"], sources=["code", "test"]),
    ]
    ranked1 = rank_candidates(candidates)
    ranked2 = rank_candidates(candidates)
    assert [t.title for t, _ in ranked1] == [t.title for t, _ in ranked2]
    assert ranked1[0][0].title == "A"
    assert ranked1[-1][0].title == "B"


def test_ranking_rewards_more_evidence():
    """A candidate with more evidence scores higher."""
    candidates = [
        _task("Less evidence", evidence=["e1"], sources=["code"]),
        _task("More evidence", evidence=["e1", "e2", "e3"], sources=["code", "test"]),
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0][0].title == "More evidence"


def test_every_emitted_task_has_evidence_and_criteria():
    """PlannedTask must carry non-empty evidence and acceptance_criteria."""
    candidates = [
        _task("Good task", evidence=["real evidence"], sources=["code"]),
    ]
    ranked = rank_candidates(candidates)
    for task, _ in ranked:
        assert task.evidence
        assert task.acceptance_criteria


def test_round_trip_serialization():
    t = _task("Round trip test", evidence=["e1", "e2"], sources=["code", "doc"])
    d = planned_task_to_dict(t)
    restored = planned_task_from_dict(d)
    assert restored.title == t.title
    assert restored.evidence == t.evidence
    assert restored.sources == t.sources
