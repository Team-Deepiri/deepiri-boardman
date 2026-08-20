"""Read real source files from a GitHub repo and grep them in-process.

Without this the agent can only reason about the *shape* of a codebase (file sizes, test
ratios) and never about the code itself — so every "find the problems" answer converges on
"big modules, thin tests". This gives it grep-level evidence: swallowed exceptions,
TODO/FIXME clusters, stray debug prints, blocking calls in async paths.

We deliberately do NOT use GitHub's /search/code endpoint: it returns 200 with zero results
for repos its index does not cover (all of Team-Deepiri's private repos today), which would
make the agent report "no defects" from an empty index — worse than having no tool at all.
Fetching the largest source files and matching locally always reflects the real code.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from boardman.github.repo_fetch import fetch_repo_file_text
from boardman.github.repo_hotspots import fetch_repo_hotspots
from boardman.settings import settings

_log = logging.getLogger(__name__)

# Real defect classes, not style nits. Each pattern is (regex, what it means).
DEFECT_PROBES: tuple[tuple[str, str, str], ...] = (
    ("bare_except", r"^\s*except\s*:", "bare except — swallows KeyboardInterrupt/SystemExit too"),
    (
        "broad_except",
        r"^\s*except Exception",
        "broad handler — hides real failures when the body only logs or passes",
    ),
    ("todo", r"\b(TODO|FIXME|HACK|XXX)\b", "unfinished or known-broken work left in code"),
    ("debug_print", r"^\s*print\(", "debug print outside the logging system"),
    (
        "blocking_sleep",
        r"^\s*time\.sleep\(",
        "synchronous sleep — only a defect if the enclosing call path is async; "
        "CHECK the caller before claiming it blocks an event loop",
    ),
    ("silent_pass", r"^\s*except[^\n]*:\s*\n\s*pass\s*$", "exception silently discarded"),
)

_MAX_FILES = 16  # fallback; settings.github_code_search_max_files wins when set
_MAX_BYTES_PER_FILE = 120_000  # fallback; settings.github_code_search_max_bytes_per_file wins


async def _fetch_source_files(
    client: httpx.AsyncClient, owner: str, repo: str, paths: list[str]
) -> list[tuple[str, str]]:
    async def one(path: str) -> tuple[str, str]:
        try:
            text = await fetch_repo_file_text(
                client,
                owner,
                repo,
                path,
                max_chars=int(
                    getattr(settings, "github_code_search_max_bytes_per_file", 0)
                    or _MAX_BYTES_PER_FILE
                ),
            )
        except (httpx.HTTPError, OSError, ValueError):
            return path, ""
        if not isinstance(text, str) or text.startswith("(file unavailable"):
            return path, ""
        return (
            path,
            text[
                : int(
                    getattr(settings, "github_code_search_max_bytes_per_file", 0)
                    or _MAX_BYTES_PER_FILE
                )
            ],
        )

    return list(await asyncio.gather(*[one(p) for p in paths]))


async def search_repo_code(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    query: str,
    *,
    limit: int = 12,
) -> dict[str, Any] | None:
    """Grep the repo's largest source files for `query` (literal, case-insensitive)."""
    if not (settings.github_pat or "").strip() or not (query or "").strip():
        return None
    hot = await fetch_repo_hotspots(
        client,
        owner,
        repo,
        top_n=int(getattr(settings, "github_code_search_max_files", 0) or _MAX_FILES),
    )
    if not hot:
        return None
    paths = [f["path"] for f in (hot.get("largest_source_files") or [])][
        : int(getattr(settings, "github_code_search_max_files", 0) or _MAX_FILES)
    ]
    files = await _fetch_source_files(client, owner, repo, paths)

    needle = query.strip().casefold()
    hits: list[dict[str, Any]] = []
    for path, text in files:
        if not text:
            continue
        matches = [
            {"line": i, "text": line.strip()[:220]}
            for i, line in enumerate(text.splitlines(), 1)
            if needle in line.casefold()
        ]
        if matches:
            hits.append({"path": path, "count": len(matches), "matches": matches[:3]})
        if len(hits) >= limit:
            break
    return {
        "ok": True,
        "query": query,
        "files_searched": [p for p, t in files if t],
        "note": f"Searched the {len(paths)} largest source files, not the whole repo.",
        "hits": hits,
    }


async def scan_repo_defects(
    client: httpx.AsyncClient, owner: str, repo: str
) -> dict[str, Any] | None:
    """Run every defect probe over the repo's largest source files, with real line numbers."""
    if not (settings.github_pat or "").strip():
        return None
    hot = await fetch_repo_hotspots(
        client,
        owner,
        repo,
        top_n=int(getattr(settings, "github_code_search_max_files", 0) or _MAX_FILES),
    )
    if not hot:
        return None
    paths = [f["path"] for f in (hot.get("largest_source_files") or [])][
        : int(getattr(settings, "github_code_search_max_files", 0) or _MAX_FILES)
    ]
    files = await _fetch_source_files(client, owner, repo, paths)

    findings: list[dict[str, Any]] = []
    for key, pattern, means in DEFECT_PROBES:
        rx = re.compile(pattern, re.MULTILINE)
        per_file: dict[str, list[dict[str, Any]]] = {}
        for path, text in files:
            if not text:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    per_file.setdefault(path, []).append(
                        {"path": path, "line": i, "text": line.strip()[:200]}
                    )
        if not per_file:
            continue
        # One example per file first, so a finding spans the codebase instead of showing
        # four hits from a single module.
        examples = [hits[0] for hits in per_file.values()][:6]
        findings.append(
            {
                "probe": key,
                "means": means,
                # A cross-file total presented next to single-file examples was read as
                # "N occurrences in THAT file" (56 claimed where the file had 46).
                "total_across_files_read": sum(len(v) for v in per_file.values()),
                "occurrences_by_file": {p: len(v) for p, v in sorted(per_file.items())},
                "examples": examples,
            }
        )

    searched = [p for p, t in files if t]
    max_bytes = int(
        getattr(settings, "github_code_search_max_bytes_per_file", 0) or _MAX_BYTES_PER_FILE
    )
    return {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "files_read": searched,
        "coverage_note": (
            f"Read the {len(searched)} largest source files (of {hot.get('source_files')} total), "
            f"up to {max_bytes:,} chars each. Counts cover only what was read — "
            'state them as "at least N", never as repo-wide totals.'
        ),
        "findings": findings,
        "guidance": (
            "These are real lines from real files. Cite path + line number and quote the line "
            "when you turn one into a finding. Counts: use occurrences_by_file for a "
            "per-file number; total_across_files_read is the sum over ALL files read and "
            "must never be attributed to a single file."
        ),
    }
