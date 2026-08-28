"""Agent tool exposing the cognition planning pipeline.

The LLM proposes candidate tasks, then calls this tool which deduplicates them against
live open issues/PRs/Plaky tasks and returns the pre-scored, deduplicated list.
"""

from __future__ import annotations

import json
import logging

from boardman.agent.tool_context import get_tool_db_session
from boardman.cognition.planning import (
    dedupe_against_existing_work,
    planned_task_from_dict,
    planned_task_to_dict,
    rank_candidates,
)

logger = logging.getLogger(__name__)


async def _planning_candidates_impl(repo: str, candidates_json: str) -> str:
    """Parse, deduplicate, rank, and return planning candidates."""
    try:
        raw = json.loads(candidates_json)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "candidates_json is not valid JSON"})

    if not isinstance(raw, list):
        return json.dumps({"ok": False, "error": "candidates_json must be a JSON array"})

    candidates = [planned_task_from_dict(d) for d in raw if isinstance(d, dict)]
    if not candidates:
        return json.dumps({"ok": False, "error": "no valid candidate objects found"})

    session = get_tool_db_session()
    open_issues: list[dict] = []
    plaky_tasks: list[dict] = []

    if session and repo:
        try:
            from boardman.agent.tools.github_tools import _github_list_open_issues

            issues_raw = await _github_list_open_issues(repo)
            parsed = json.loads(issues_raw) if isinstance(issues_raw, str) else issues_raw
            if isinstance(parsed, list):
                open_issues = parsed
            elif isinstance(parsed, dict) and isinstance(parsed.get("issues"), list):
                open_issues = parsed["issues"]
        except Exception:  # noqa: BLE001
            pass

        try:
            from boardman.plaky.client import PlakyClient

            plaky = PlakyClient()
            result = await plaky.get_tasks(status="open")
            if result.get("ok"):
                plaky_tasks = result.get("tasks") or []
        except Exception:  # noqa: BLE001
            pass

    existing_titles: set[str] = set()
    for issue in open_issues:
        t = str(issue.get("title") or "").strip().casefold()
        if t:
            existing_titles.add(t)
    for task in plaky_tasks:
        t = str(task.get("name") or task.get("title") or "").strip().casefold()
        if t:
            existing_titles.add(t)

    deduped = dedupe_against_existing_work(candidates, open_issues, [], plaky_tasks)
    ranked = rank_candidates(deduped)

    result = []
    for task, breakdown in ranked:
        entry = planned_task_to_dict(task)
        entry["score_breakdown"] = breakdown
        result.append(entry)

    return json.dumps({"ok": True, "candidates": result, "dropped": len(candidates) - len(deduped)})


def planning_candidates_tool():
    """Build the LangChain tool for planning candidates."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class PlanningCandidatesInput(BaseModel):
        repo: str = Field(description="Full repo name (owner/repo)")
        candidates_json: str = Field(
            description="JSON array of candidate tasks with title, problem, evidence, sources, acceptance_criteria, verification_method"
        )

    return StructuredTool.from_function(
        coroutine=_planning_candidates_impl,
        name="planning_candidates",
        description=(
            "Deduplicate and rank proposed planning tasks against existing open "
            "GitHub issues, PRs, and Plaky tasks. Call this with your proposed tasks "
            "before presenting them to the user."
        ),
        args_schema=PlanningCandidatesInput,
    )
