"""Agent tools: preview QA assignment for a repo."""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from boardman.assignment.qa_picker import build_assignment_field_map, pick_qa_for_repo


async def _assignment_preview(owner_repo: str) -> str:
    """JSON: chosen QA id, Plaky field map, and reason."""
    qid, qwhy = await pick_qa_for_repo(owner_repo)
    fm = await build_assignment_field_map(owner_repo)
    return json.dumps(
        {
            "ok": True,
            "owner_repo": owner_repo.strip(),
            "qa_plaky_id": qid,
            "qa_reason": qwhy,
            "plaky_field_values": fm,
        },
        indent=2,
    )[:8000]


def assignment_preview_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_assignment_preview,
        name="assignment_preview",
        description=(
            "Preview semi-random QA Plaky id and field map for a GitHub owner/repo. "
            "Roster comes from the GitHub support org team + member_overrides in team_assignments.yml "
            "(unless the YAML uses an explicit members: list). "
            "Does not write to Plaky. Args: owner_repo."
        ),
    )
