"""Collect markdown and doc excerpts from a local project tree for AI task suggestions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache", ".tox"}
)
DOC_NAMES = frozenset(
    {
        "readme.md",
        "direction.md",
        "spec.md",
        "contributing.md",
        "changelog.md",
    }
)
def _under_skip_parts(rel_parts: tuple[str, ...]) -> bool:
    return bool(set(rel_parts) & SKIP_DIRS) or any(p in SKIP_DIRS for p in rel_parts)


def gather_local_scan_context(
    path: str,
    *,
    max_doc_files: int = 50,
    excerpt_chars: int = 6000,
    max_direction_chars: int = 48000,
) -> Dict[str, Any]:
    """
    Read DIRECTION.md, README, and other docs under ``docs/`` (bounded).

    Returns a dict with ``ok``, ``root``, ``direction_md``, ``readme_excerpt``, ``doc_excerpts``,
    and ``top_level`` (names of immediate children, capped).
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "message": f"Not a directory: {root}"}

    direction_md = ""
    for name in ("DIRECTION.md", "direction.md"):
        p = root / name
        if p.is_file():
            try:
                direction_md = p.read_text(encoding="utf-8", errors="replace")[:max_direction_chars]
            except OSError:
                direction_md = ""
            break

    readme_excerpt = ""
    for name in ("README.md", "README.rst", "readme.md"):
        p = root / name
        if p.is_file():
            try:
                readme_excerpt = p.read_text(encoding="utf-8", errors="replace")[:excerpt_chars]
            except OSError:
                readme_excerpt = ""
            break

    doc_excerpts: List[Dict[str, str]] = []
    count = 0
    for cur, dirs, files in os.walk(root, topdown=True):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        try:
            rel_parts = Path(cur).resolve().relative_to(root).parts
        except ValueError:
            continue
        if _under_skip_parts(rel_parts):
            continue
        for name in sorted(files):
            if count >= max_doc_files:
                break
            p = Path(cur) / name
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(root).as_posix())
            except ValueError:
                continue
            if _under_skip_parts(tuple(Path(rel).parts)):
                continue
            low = name.lower()
            rel_low = rel.lower()
            is_doc = (
                low in DOC_NAMES
                or rel_low.startswith("docs/")
                or "/docs/" in rel_low
                or rel_low.endswith(".md")
            )
            parts_n = len(Path(rel).parts)
            if parts_n == 1 and rel_low in ("readme.md", "readme.rst", "direction.md"):
                continue
            if not is_doc:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                continue
            doc_excerpts.append({"path": rel, "excerpt": text[:excerpt_chars]})
            count += 1

    top_names: List[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda x: x.name.lower())[:48]:
            if child.name.startswith("."):
                continue
            top_names.append(child.name + ("/" if child.is_dir() else ""))
    except OSError:
        top_names = []

    return {
        "ok": True,
        "root": str(root),
        "project_name": root.name,
        "direction_md": direction_md.strip(),
        "readme_excerpt": readme_excerpt.strip(),
        "doc_excerpts": doc_excerpts,
        "top_level": top_names,
    }
