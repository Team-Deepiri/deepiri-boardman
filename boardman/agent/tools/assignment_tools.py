"""Agent tools: preview QA assignment for a repo."""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from boardman.assignment.qa_picker import (
    build_assignment_field_map,
    ensure_github_owner_repo,
    pick_qa_for_repo,
)


async def _assignment_preview(owner_repo: str) -> str:
    """JSON: chosen QA id, Plaky field map, and reason."""
    # Bare names ("diri-cyrex") get the configured owner so roster globs can match.
    full = ensure_github_owner_repo((owner_repo or "").strip())
    qid, qwhy = await pick_qa_for_repo(full)
    fm = await build_assignment_field_map(full)
    return json.dumps(
        {
            "ok": True,
            "owner_repo": full,
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
            "Preview which QA the assignment algorithm picks for a GitHub repo, with the full "
            "scored reasoning (tier filter, GitHub contribution fit via cosine similarity, ranking) "
            "and the Plaky field map. Roster comes from the GitHub support org team + "
            "member_overrides in team_assignments.yml. Does not write to Plaky. "
            "Args: owner_repo (owner/name preferred; bare names get the default owner)."
        ),
    )
