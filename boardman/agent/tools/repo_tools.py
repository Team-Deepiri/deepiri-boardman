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
MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "dockerfile",
        "makefile",
    }
)
_MAX_WALK = 5000  # stop walking huge trees; enough to summarize structure


def _scan_local_repo(path: str, max_files: int = 40) -> str:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return json.dumps({"ok": False, "message": f"Not a directory: {root}"})

    found: List[dict] = []
    manifests: List[dict] = []
    top_level: dict[str, int] = {}
    walked = 0
    count = 0
    for p in root.rglob("*"):
        if count >= max_files or walked >= _MAX_WALK:
            break
        if any(x in SKIP_DIRS for x in p.parts):
            continue
        if not p.is_file():
            continue
        walked += 1
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            continue
        head = rel.replace("\\", "/").split("/", 1)[0]
        top_level[head] = top_level.get(head, 0) + 1
        low = p.name.lower()
        rel_low = rel.lower().replace("\\", "/")
        if low in DOC_NAMES or rel_low.startswith("docs/") or "/docs/" in rel_low:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            found.append({"path": rel, "excerpt": text[:4000]})
            count += 1
        elif low in MANIFEST_NAMES and len(manifests) < 6:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:2500]
            except OSError:
                continue
            manifests.append({"path": rel, "excerpt": text})

    out: dict = {"ok": True, "root": str(root), "files": found}
    if not found:
        # No markdown docs anywhere — fall back to structure so the agent can still
        # explain the repo: top-level layout + manifest excerpts.
        out["message"] = (
            "No README/DIRECTION.md/docs found. Explain the repo from structure_summary and "
            "manifests below; read specific source files if more depth is needed."
        )
        out["structure_summary"] = dict(sorted(top_level.items(), key=lambda kv: -kv[1])[:25])
        out["manifests"] = manifests
    return json.dumps(out, indent=2)[:14000]


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
