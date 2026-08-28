"""Agent tools: preview QA and developer assignment for a repo."""

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


async def _developer_pick(owner_repo: str) -> str:
    """JSON: best-fit developer for the repo, ranked list, and reasoning."""
    from boardman.assignment.developer_picker import pick_developer_for_repo

    full = ensure_github_owner_repo((owner_repo or "").strip())
    name, reason, ranked = await pick_developer_for_repo(full)
    return json.dumps(
        {
            "ok": name is not None,
            "owner_repo": full,
            "developer": name,
            "reason": reason,
            "ranked": ranked,
        },
        indent=2,
    )[:8000]


def assignment_preview_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_assignment_preview,
        name="assignment_preview",
        description=(
            "Preview which QA the assignment algorithm picks for a GitHub repo. Returns a "
            "human-readable explanation of why that person was chosen (experience, language "
            "overlap, related projects) along with a confidence percentage and the Plaky field "
            "map. Does not write to Plaky. "
            "Args: owner_repo (owner/name preferred; bare names get the default owner)."
        ),
    )


def developer_pick_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_developer_pick,
        name="developer_pick",
        description=(
            "Pick the best-fit developer to assign to a task for a GitHub repo. Returns a "
            "human-readable explanation of why that person was chosen, a confidence percentage, "
            "and a ranked list of other candidates with their strengths. Use this when the user "
            "asks you to assign someone or pick an assignee. "
            "Args: owner_repo (owner/name preferred; bare names get the default owner)."
        ),
    )
