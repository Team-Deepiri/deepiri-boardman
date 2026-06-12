"""Local repo scan tool for the agent."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

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
TEXT_EXTS = frozenset(
    {
        ".md",
        ".txt",
        ".rst",
        ".py",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".sh",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
    }
)
_TODO_RE = re.compile(r"\bTODO\b")
_FIXME_RE = re.compile(r"\bFIXME\b")

MANIFEST_NAMES = (
    "pyproject.toml",
    "package.json",
    "poetry.lock",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/test.yml",
)


def _iter_repo_files(root: Path) -> Iterator[Tuple[Path, str]]:
    for cur_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_dir = Path(cur_dir).relative_to(root)
        for name in sorted(files):
            p = Path(cur_dir) / name
            rel = str((rel_dir / name).as_posix()).lstrip("./")
            yield p, rel


def _read_file_once(p: Path, max_limit: int) -> str:
    """Read at most ``max_limit`` chars in one syscall; reuse slices for smaller excerpts."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_limit]


def _is_textish(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    low = path.name.lower()
    return low in DOC_NAMES or low in {"dockerfile", "makefile"}


def _scan_local_repo(path: str, max_files: int = 40) -> str:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return json.dumps({"ok": False, "message": f"Not a directory: {root}"})

    docs: List[Dict[str, str]] = []
    manifests: List[Dict[str, str]] = []
    top_dirs: set[str] = set()
    top_files: List[str] = []
    todo_lines = 0
    fixme_lines = 0
    scanned_text_files = 0

    manifest_paths = {m.lower() for m in MANIFEST_NAMES}
    for p, rel in _iter_repo_files(root):
        parts = Path(rel).parts
        if parts:
            if len(parts) > 1:
                top_dirs.add(parts[0])
            elif "." not in parts[0]:
                top_dirs.add(parts[0])
        if len(parts) == 1 and len(top_files) < 50:
            top_files.append(rel)

        rel_low = rel.lower()
        name_low = p.name.lower()
        is_doc = (
            name_low in DOC_NAMES
            or rel_low.startswith("docs/")
            or "/docs/" in rel_low
            or rel_low.endswith(".md")
        )
        is_manifest = rel_low in manifest_paths and len(manifests) < 20
        is_todo_sample = _is_textish(p) and scanned_text_files < 220

        if is_doc or is_manifest or is_todo_sample:
            need = 6000 if is_todo_sample else max(4000 if is_doc else 0, 2000 if is_manifest else 0)
            if need <= 0:
                need = 4000
            raw = _read_file_once(p, need)
            if not raw:
                continue
            if is_doc and len(docs) < max(1, int(max_files)):
                docs.append({"path": rel, "excerpt": raw[:4000]})
            if is_manifest:
                manifests.append({"path": rel, "excerpt": raw[:2000]})
            if is_todo_sample:
                scanned_text_files += 1
                todo_lines += len(_TODO_RE.findall(raw))
                fixme_lines += len(_FIXME_RE.findall(raw))

    docs = sorted({d["path"]: d for d in docs}.values(), key=lambda x: x["path"])
    manifests = sorted({m["path"]: m for m in manifests}.values(), key=lambda x: x["path"])
    top_files = sorted(set(top_files))

    has_direction = any(d["path"].lower().endswith("direction.md") for d in docs)
    has_readme = any(d["path"].lower().endswith("readme.md") for d in docs)

    out: Dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "repo_map": {
            "top_level_dirs": sorted(top_dirs)[:80],
            "top_level_files": top_files[:80],
        },
        "docs": {
            "direction_present": has_direction,
            "readme_present": has_readme,
            "count": len(docs),
            "files": docs,
        },
        "manifests": manifests,
        "todo_summary": {
            "todo_lines": todo_lines,
            "fixme_lines": fixme_lines,
            "text_files_sampled": scanned_text_files,
        },
        "files": docs,
    }
    return json.dumps(out, indent=2)[:18000]


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
            "Scan a local repo path and return structured context: top-level map, key docs, "
            "manifest excerpts, and TODO/FIXME signal counts. "
            "Args: path (absolute or relative), max_files (default 40 for docs)."
        ),
    )
