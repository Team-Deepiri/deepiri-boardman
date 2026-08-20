"""Compact persistent repository context for the Boardman assistant.

The in-process GitHub read cache removes duplicate requests during one process lifetime.
This module adds the complementary ProjectContext snapshot: a bounded, structured planning
payload that survives an API restart without turning the database into a repository mirror.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import ProjectContext
from boardman.settings import settings


def _repo_key(repo: str) -> str:
    return (repo or "").strip().casefold()


def _payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


async def load_planning_snapshot(
    session: AsyncSession | None,
    repo: str,
    *,
    allow_stale: bool = False,
) -> tuple[str | None, str]:
    """Return ``(json_payload, cache_state)`` for a repo snapshot.

    ``cache_state`` is ``miss``, ``fresh``, or ``stale``. Stale snapshots are only
    returned when explicitly requested by the caller as a degraded-mode fallback.
    """

    from boardman.observability.counters import bump, cache_hit, cache_miss

    key = _repo_key(repo)
    if session is None or not key:
        cache_miss("project_context")
        return None, "miss"

    try:
        row = (
            await session.execute(select(ProjectContext).where(ProjectContext.repo == repo))
        ).scalar_one_or_none()
    except SQLAlchemyError:
        # A rolling deployment may run one API process before migration 005 has been
        # applied. Context is an optimization; never make chat unavailable because its
        # optional columns are not present yet.
        await session.rollback()
        return None, "unavailable"
    if row is None or not row.context_json or row.context_fetched_at is None:
        cache_miss("project_context")
        return None, "miss"

    try:
        payload = json.loads(row.context_json)
    except (TypeError, ValueError):
        return None, "miss"
    if not isinstance(payload, dict) or not payload.get("ok"):
        cache_miss("project_context")
        return None, "miss"

    age = datetime.utcnow() - row.context_fetched_at
    fresh = age <= timedelta(seconds=max(0.0, settings.agent_repo_context_cache_ttl_seconds))
    stale_ok = age <= timedelta(
        seconds=max(
            settings.agent_repo_context_cache_ttl_seconds,
            settings.agent_repo_context_stale_if_error_seconds,
        )
    )
    if fresh:
        cache_hit("project_context")
        payload.setdefault("cache", {})
        payload["cache"].update(
            {"state": "persistent-hit", "age_seconds": round(age.total_seconds(), 1)}
        )
        return _payload_text(payload), "fresh"
    if allow_stale and stale_ok:
        # A stale serve is still a hit for latency purposes; counted apart so the two are
        # never confused when reading a benchmark.
        cache_hit("project_context")
        bump("cache.project_context.stale_served")
        payload.setdefault("cache", {})
        payload["cache"].update(
            {"state": "stale-fallback", "age_seconds": round(age.total_seconds(), 1)}
        )
        return _payload_text(payload), "stale"
    cache_miss("project_context")
    return None, "miss"


async def save_planning_snapshot(
    session: AsyncSession | None,
    repo: str,
    payload: dict[str, Any],
    *,
    source_revision: str = "",
) -> None:
    """Upsert a bounded planning payload; the caller owns the transaction commit."""

    key = _repo_key(repo)
    if session is None or not key or not isinstance(payload, dict) or not payload.get("ok"):
        return

    try:
        row = (
            await session.execute(select(ProjectContext).where(ProjectContext.repo == repo))
        ).scalar_one_or_none()
    except SQLAlchemyError:
        await session.rollback()
        return
    now = datetime.utcnow()
    if row is None:
        row = ProjectContext(repo=repo)
        session.add(row)
    row.context_json = _payload_text(payload)
    row.context_source_revision = (source_revision or "").strip()[:255] or None
    row.context_fetched_at = now
    if not row.summary:
        direction = payload.get("DIRECTION_md")
        row.summary = str(direction or "")[:12_000] or None
    row.last_scanned = row.last_scanned or now


#: Snapshot fields a partial writer must never blank out. The scan knows about DIRECTION,
#: commits, issues and Plaky tasks; it knows nothing about the tree, the README or the
#: code signals, and it must not answer for them.
_RICH_FIELDS = (
    "structure",
    "readme_md",
    "code_signals",
    "open_pull_requests_markdown",
    "notable_files",
    "hotspots_markdown",
)


def merge_planning_snapshot(existing_json: str | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Overlay a partial snapshot onto whatever is already stored.

    Two writers share this row: the planning-context tool, which fetches everything, and
    the repo scan, which fetches four things and used to write a stub for the rest. The
    stub won: it stamped a fresh timestamp, so for the next fifteen minutes the assistant's
    default context said ``Default branch: unknown`` about a repo it had fully read an hour
    earlier. A partial write may add and replace what it knows, never blank what it does not.
    """
    base: dict[str, Any] = {}
    if existing_json:
        try:
            loaded = json.loads(existing_json)
            if isinstance(loaded, dict) and loaded.get("ok"):
                base = loaded
        except (TypeError, ValueError):
            base = {}

    merged = dict(base)
    for key, value in incoming.items():
        if key in _RICH_FIELDS and not _has_content(value) and _has_content(base.get(key)):
            continue  # the incoming writer does not know this field; keep what we have
        merged[key] = value
    return merged


def _has_content(value: Any) -> bool:
    """True when a snapshot field actually says something.

    ``{"default_branch": "unknown", "top_level_dirs": [], "important_paths": []}`` is a
    shaped placeholder, not knowledge, and it has to lose to a real one.
    """
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        real = {
            k: v
            for k, v in value.items()
            if v not in (None, "", [], {}, "unknown", "unavailable", "none")
        }
        return bool(real)
    if isinstance(value, str):
        return bool(value.strip()) and not value.strip().startswith("(")
    return True


def snapshot_prompt_block(payload_json: str | None, *, max_chars: int = 6500) -> str:
    """Render only high-signal cached facts for a plain LLM turn."""

    if not payload_json:
        return ""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return ""

    structure = payload.get("structure") if isinstance(payload.get("structure"), dict) else {}
    lines = [
        "\n## Cached repository context (compact; refresh with tools for deeper analysis)",
        f"- Repo: `{payload.get('repo') or ''}`",
        f"- Description: {structure.get('description') or 'unknown'}",
        f"- Language: {structure.get('language') or 'unknown'}",
        f"- Topics: {', '.join(str(x) for x in (structure.get('topics') or [])[:8]) or 'none'}",
        f"- Default branch: {structure.get('default_branch') or 'unknown'}",
        f"- Top-level structure: {', '.join(str(x) for x in (structure.get('top_level_dirs') or [])[:12]) or 'unknown'}",
        f"- Important paths: {', '.join(str(x) for x in (structure.get('important_paths') or [])[:20]) or 'none'}",
    ]
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    if routing:
        lines.append(
            f"- Plaky routing: board `{routing.get('board_id') or 'not configured'}`, "
            f"group `{routing.get('group_id') or 'not configured'}`"
        )
    for label, key, cap in (
        ("Direction", "DIRECTION_md", 2200),
        ("README", "readme_md", 2200),
        ("Open issues", "open_issues_markdown", 900),
        ("Open PRs", "open_pull_requests_markdown", 900),
        ("Recent commits", "recent_commits_markdown", 700),
        ("Existing Plaky tasks", "plaky_tasks_markdown", 700),
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith("("):
            lines.append(f"\n### {label}\n{value[:cap].strip()}")
    text = "\n".join(lines)
    return text[:max_chars]
