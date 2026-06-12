#!/usr/bin/env python3
"""Lightweight AGENTS.md drift checks. Exit 0 on pass, 1 on hard failures."""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
DOCS = ROOT / "docs"
STALE_DAYS = 90

# Paths that are templates, examples, or external references — skip existence check.
SKIP_PATHS = {
    "CLAUDE.md",
    "poetry.lock",
    ".env",
    ".env.production.example",
}


def _looks_like_repo_path(raw: str) -> bool:
    if not raw or " " in raw or "://" in raw or raw.startswith("..."):
        return False
    if raw.startswith(("boardman/", "docs/", "scripts/", "tests/", "alembic/", "deploy/", "worker/")):
        return True
    if "/" in raw:
        return True
    # Root-level config/docs only (not bare module filenames like runner.py).
    return raw.endswith((".md", ".yml", ".toml", ".example")) and "/" not in raw


def _extract_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for m in re.finditer(r"`([^`]+)`", text):
        raw = m.group(1).strip().split("#")[0]
        if _looks_like_repo_path(raw):
            paths.add(raw)
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        target = m.group(2).strip().split("#")[0]
        if target.startswith("http") or target.startswith("#"):
            continue
        if _looks_like_repo_path(target):
            paths.add(target)
    return paths


def _last_verified(text: str) -> date | None:
    m = re.search(r"Last verified:\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    if not AGENTS.is_file():
        print("FAIL: AGENTS.md missing", file=sys.stderr)
        return 1

    text = AGENTS.read_text(encoding="utf-8")

    for rel in sorted(_extract_paths(text)):
        if rel in SKIP_PATHS:
            continue
        path = ROOT / rel
        if not path.exists():
            errors.append(f"broken path reference: {rel}")

    verified = _last_verified(text)
    if verified is None:
        warnings.append("missing or unparseable 'Last verified' date")
    elif (date.today() - verified).days > STALE_DAYS:
        warnings.append(f"Last verified is {verified} (>{STALE_DAYS} days ago)")

    doc_md = {p.relative_to(ROOT).as_posix() for p in DOCS.glob("*.md")}
    indexed = set(re.findall(r"docs/[A-Za-z0-9_./-]+\.md", text))
    missing_from_index = sorted(doc_md - indexed - {"docs/AGENTS_MAINTENANCE.md"})
    if missing_from_index:
        warnings.append("docs not in AGENTS.md index: " + ", ".join(missing_from_index))

    if warnings:
        for w in warnings:
            print(f"WARN: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1

    print("OK: AGENTS.md verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
