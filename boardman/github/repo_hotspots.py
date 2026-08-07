"""Code-level repo signals from ONE GitHub tree call.

The planning context reads docs, issues and commits — good for direction, useless for
"find the real problems in this repo", which needs *source* evidence. This module derives
that evidence from a single recursive tree fetch (blob sizes are included), so it stays
cheap enough to run on every audit-style question.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from boardman.settings import settings

_log = logging.getLogger(__name__)

_SOURCE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".kt", ".scala", ".sh",
}
_TEST_MARKERS = ("test_", "_test.", "/tests/", "/test/", ".spec.", ".test.")
_VENDOR_MARKERS = ("node_modules/", "vendor/", "site-packages/", "dist/", "build/", ".venv/")

# Things that should essentially never be committed. Each is a finding on its own.
_ARTIFACT_RULES: tuple[tuple[str, str], ...] = (
    (".env", "environment file with likely secrets is tracked in git"),
    (".db", "SQLite database committed to the repo"),
    (".sqlite", "SQLite database committed to the repo"),
    (".db-wal", "SQLite write-ahead log committed to the repo"),
    (".db-shm", "SQLite shared-memory file committed to the repo"),
    (".pem", "private key material tracked in git"),
    (".p12", "keystore tracked in git"),
    ("id_rsa", "private SSH key tracked in git"),
)


def _is_source(path: str) -> bool:
    low = path.lower()
    if any(v in low for v in _VENDOR_MARKERS):
        return False
    return any(low.endswith(e) for e in _SOURCE_EXTS)


def _is_test(path: str) -> bool:
    low = path.lower()
    return any(m in low for m in _TEST_MARKERS)


async def fetch_repo_hotspots(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    *,
    branch: str = "",
    top_n: int = 15,
) -> Optional[dict[str, Any]]:
    """Largest source files, test ratio, directory weight, and tracked-artifact smells."""
    token = (settings.github_pat or "").strip()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    ref = (branch or "").strip()
    if not ref:
        try:
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}", headers=headers, follow_redirects=True
            )
            if r.status_code != 200:
                return None
            ref = str((r.json() or {}).get("default_branch") or "main")
        except Exception as e:  # noqa: BLE001
            _log.debug("hotspots: default branch lookup failed for %s/%s: %s", owner, repo, e)
            return None

    try:
        tr = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1",
            headers=headers,
            follow_redirects=True,
        )
        if tr.status_code != 200:
            return None
        data = tr.json() or {}
    except Exception as e:  # noqa: BLE001
        _log.debug("hotspots: tree fetch failed for %s/%s: %s", owner, repo, e)
        return None

    blobs = [n for n in (data.get("tree") or []) if isinstance(n, dict) and n.get("type") == "blob"]
    source: list[tuple[str, int]] = []
    tests = 0
    dir_counts: dict[str, int] = {}
    artifacts: list[dict[str, str]] = []

    for n in blobs:
        path = str(n.get("path") or "")
        if not path:
            continue
        size = int(n.get("size") or 0)
        low = path.lower()
        base = low.rsplit("/", 1)[-1]

        for marker, why in _ARTIFACT_RULES:
            # ".env" must not match ".env.example"; suffix rules match anywhere in the name.
            hit = base == marker if marker == ".env" else (base.endswith(marker) or marker in base)
            if hit and not base.endswith((".example", ".sample", ".template")):
                artifacts.append({"path": path, "why": why})
                break

        if _is_source(path):
            top = path.split("/", 1)[0] if "/" in path else "(root)"
            dir_counts[top] = dir_counts.get(top, 0) + 1
            if _is_test(path):
                tests += 1
            else:
                source.append((path, size))

    source.sort(key=lambda t: -t[1])
    src_count = len(source)
    return {
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "truncated": bool(data.get("truncated")),
        "total_files": len(blobs),
        "source_files": src_count,
        "test_files": tests,
        "test_to_source_ratio": round(tests / src_count, 2) if src_count else 0.0,
        # Size in bytes; ~35 bytes/line is a decent LOC proxy across these languages.
        "largest_source_files": [
            {"path": p, "bytes": s, "approx_lines": max(1, s // 35)} for p, s in source[:top_n]
        ],
        "files_by_top_dir": dict(sorted(dir_counts.items(), key=lambda kv: -kv[1])[:12]),
        "tracked_artifacts": artifacts[:20],
    }
