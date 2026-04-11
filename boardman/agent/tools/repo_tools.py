"""Local repo scan tool for the agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from langchain_core.tools import StructuredTool

DOC_NAMES = frozenset(
    {
        "readme.md",
        "direction.md",
        "spec.md",
        "contributing.md",
        "changelog.md",
    }
)
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache"}
)


def _scan_local_repo(path: str, max_files: int = 40) -> str:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return json.dumps({"ok": False, "message": f"Not a directory: {root}"})

    found: List[dict] = []
    count = 0
    for p in root.rglob("*"):
        if count >= max_files:
            break
        if not p.is_file():
            continue
        parts = set(p.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        if any(x in SKIP_DIRS for x in p.parts):
            continue
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            continue
        low = p.name.lower()
        if low in DOC_NAMES or rel.lower().startswith("docs/") or "/docs/" in rel.lower():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            found.append({"path": rel, "excerpt": text[:4000]})
            count += 1

    return json.dumps({"ok": True, "root": str(root), "files": found}, indent=2)[:14000]


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
            "Read key docs from a local filesystem path (README, DIRECTION.md, docs/, etc.). "
            "Args: path (absolute or relative), max_files (default 40)."
        ),
    )
