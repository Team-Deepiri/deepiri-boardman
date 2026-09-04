"""Structural signals that say "this PR is not something to make/link a Plaky task for."

Deliberately data/config-driven, not a name/keyword allowlist:
  - bot authorship is detected from GitHub's own `user.type == "Bot"` field and the
    `login` ending in the `[bot]` suffix GitHub stamps on every bot/App account
    (dependabot[bot], a repo's own GitHub App, etc.) — no specific bot name is hardcoded,
    so this covers dependabot, renovate, a custom GitHub App, or any future bot the same way.
  - branch-pair skipping is driven by two configurable branch names
    (`settings.pr_task_sync_integration_branch_a`/`_b`, default main/dev) — a PR is
    skipped ONLY when {base, head} is exactly that pair (dev->main or main->dev), never
    for a PR merely touching one of those branches on the other side (e.g. a feature
    branch merging into dev still gets a task; only a dev<->main merge-back does not).
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


def is_integration_branch_pair(base_ref: str, head_ref: str) -> bool:
    """True ONLY when {base, head} is exactly the configured dev<->main pair, in either
    direction — never for a PR that merely has one of those branches as base or head."""
    a = (settings.pr_task_sync_integration_branch_a or "").strip().casefold()
    b = (settings.pr_task_sync_integration_branch_b or "").strip().casefold()
    if not a or not b:
        return False
    base = (base_ref or "").strip().casefold()
    head = (head_ref or "").strip().casefold()
    if not base or not head:
        return False
    return {base, head} == {a, b}


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
