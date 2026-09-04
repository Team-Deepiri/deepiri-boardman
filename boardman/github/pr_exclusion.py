"""Structural signals that say "this PR is not something to make/link a Plaky task for."

Deliberately data/config-driven, not a name/keyword allowlist:
  - bot authorship is detected from GitHub's own `user.type == "Bot"` field and the
    `login` ending in the `[bot]` suffix GitHub stamps on every bot/App account
    (dependabot[bot], a repo's own GitHub App, etc.) — no specific bot name is hardcoded,
    so this covers dependabot, renovate, a custom GitHub App, or any future bot the same way.
  - branch-pair skipping is driven by `settings.pr_task_sync_skip_branches`, a configurable
    set of "integration" branch names (default main, dev) — a PR whose base AND head are
    both in that set (e.g. dev->main, main->dev) is a merge-back between long-lived
    branches, not new work, in either direction.
"""

from __future__ import annotations

from boardman.settings import settings


def _is_bot_actor(user: dict | None) -> bool:
    if not isinstance(user, dict):
        return False
    if str(user.get("type") or "").strip().casefold() == "bot":
        return True
    login = str(user.get("login") or "").strip().casefold()
    return login.endswith("[bot]")


def _skip_branch_set() -> set[str]:
    raw = settings.pr_task_sync_skip_branches or ""
    return {b.strip().casefold() for b in raw.split(",") if b.strip()}


def is_integration_branch_pair(base_ref: str, head_ref: str) -> bool:
    """True when base and head are both configured "integration" branches (dev<->main)."""
    skip = _skip_branch_set()
    if not skip:
        return False
    b = (base_ref or "").strip().casefold()
    h = (head_ref or "").strip().casefold()
    if not b or not h:
        return False
    return b in skip and h in skip


def pr_sync_exclusion_reason(
    *,
    base_ref: str,
    head_ref: str,
    pr_user: dict | None,
) -> str:
    """Reason string if this PR should be skipped entirely (no task created, no linking
    run), or "" if the PR should be processed normally."""
    if settings.pr_task_sync_skip_bot_authors and _is_bot_actor(pr_user):
        login = str((pr_user or {}).get("login") or "").strip() or "unknown"
        return f"PR author '{login}' is a bot account"
    if is_integration_branch_pair(base_ref, head_ref):
        return f"PR is a branch-integration merge ({head_ref} -> {base_ref}), not new work"
    return ""
