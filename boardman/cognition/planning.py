"""Evidence-backed, deduplicated planning engine.

Candidates are proposed by the LLM, then this module deduplicates them against open
GitHub/Plaky work and scores them deterministically using source diversity and evidence
count. Follows the TaskCandidate/ScoredCandidate shape from pr_task_linking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlannedTask:
    title: str
    problem: str
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_method: str = ""


def dedupe_against_existing_work(
    candidates: list[PlannedTask],
    open_issues: list[dict],
    open_prs: list[dict],
    plaky_tasks: list[dict],
) -> list[PlannedTask]:
    """Remove candidates whose title collides with existing open work."""
    existing_titles: set[str] = set()
    for issue in open_issues:
        title = str(issue.get("title") or "").strip().casefold()
        if title:
            existing_titles.add(title)
    for pr in open_prs:
        title = str(pr.get("title") or "").strip().casefold()
        if title:
            existing_titles.add(title)
    for task in plaky_tasks:
        title = str(task.get("name") or task.get("title") or "").strip().casefold()
        if title:
            existing_titles.add(title)

    result: list[PlannedTask] = []
    for c in candidates:
        key = c.title.strip().casefold()
        if key in existing_titles:
            continue
        if any(_titles_overlap(key, et) for et in existing_titles):
            continue
        result.append(c)
    return result


def _titles_overlap(a: str, b: str) -> bool:
    """True when two casefolded titles are similar enough to be duplicates."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter and shorter in longer:
        return True
    return False


def rank_candidates(
    candidates: list[PlannedTask],
) -> list[tuple[PlannedTask, dict[str, float]]]:
    """Score and sort candidates deterministically. Assumes dedup already ran."""
    scored: list[tuple[PlannedTask, dict[str, float]]] = []
    for c in candidates:
        breakdown: dict[str, float] = {}

        breakdown["evidence_count"] = min(len(c.evidence) * 10.0, 50.0)

        source_types = set(c.sources)
        breakdown["source_diversity"] = min(len(source_types) * 15.0, 45.0)

        breakdown["total"] = sum(breakdown.values())
        scored.append((c, breakdown))

    scored.sort(key=lambda x: x[1]["total"], reverse=True)
    return scored


def planned_task_to_dict(t: PlannedTask) -> dict[str, Any]:
    return {
        "title": t.title,
        "problem": t.problem,
        "evidence": t.evidence,
        "sources": t.sources,
        "acceptance_criteria": t.acceptance_criteria,
        "verification_method": t.verification_method,
    }


def planned_task_from_dict(d: dict[str, Any]) -> PlannedTask:
    return PlannedTask(
        title=str(d.get("title") or "").strip(),
        problem=str(d.get("problem") or "").strip(),
        evidence=d.get("evidence") if isinstance(d.get("evidence"), list) else [],
        sources=d.get("sources") if isinstance(d.get("sources"), list) else [],
        acceptance_criteria=(
            d.get("acceptance_criteria") if isinstance(d.get("acceptance_criteria"), list) else []
        ),
        verification_method=str(d.get("verification_method") or ""),
    )
