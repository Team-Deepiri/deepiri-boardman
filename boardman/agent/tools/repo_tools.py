"""Local repo scan tool for the agent."""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from boardman.services.local_scan_context import gather_local_scan_context

_OUTPUT_CAP = 14000


def _scan_local_repo(path: str, max_files: int = 40) -> str:
    """Bounded read of DIRECTION.md, README, docs/, markdown — same rules as batch local scan."""
    bundle = gather_local_scan_context(
        path,
        max_doc_files=max_files,
        excerpt_chars=4000,
    )
    if not bundle.get("ok"):
        return json.dumps(
            {"ok": False, "message": bundle.get("message", "invalid path")},
            indent=2,
        )
    payload = {
        "ok": True,
        "root": bundle.get("root"),
        "project_name": bundle.get("project_name"),
        "direction_md": bundle.get("direction_md") or "",
        "readme_excerpt": bundle.get("readme_excerpt") or "",
        "top_level": bundle.get("top_level") or [],
        "files": bundle.get("doc_excerpts") or [],
    }
    return json.dumps(payload, indent=2)[:_OUTPUT_CAP]


def thoughts_tool() -> StructuredTool:
    return StructuredTool.from_function(
        lambda thought: f"Thought recorded: {thought[:50]}...",
        name="thoughts",
        description=(
            "Record your internal plan, reasoning, or multi-step logic here. "
            "Use this for private scratchpad thinking before you act or reply. "
            "Output to this tool is NOT visible to the user as chat text."
        ),
    )


def scan_local_repo_tool() -> StructuredTool:
    return StructuredTool.from_function(
        _scan_local_repo,
        name="scan_local_repo",
        description=(
            "Read key docs from a local filesystem path (DIRECTION.md, README, docs/, other .md). "
            "Returns direction_md, readme_excerpt, files (path+excerpt list), top_level. "
            "Args: path (absolute or ~), max_files (default 40, caps doc entries in files)."
        ),
    )
