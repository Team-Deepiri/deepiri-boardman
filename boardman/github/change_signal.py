"""One place that answers "did this event change what we know about the repo?".

GitHub tells us the moment a repo changes; the periodic sweep is only the net that
catches what the webhook missed. Acting on the event is what makes cached knowledge safe
to serve — without it, a cache long enough to be useful is also long enough to be wrong.

Two callers on purpose: the webhook route and the poller. They are two independent copies
of the same dispatch table and they already differ, so a hook written into only one of
them would leave the live poller session serving stale context that looks correct in
tests.

Every call here is synchronous, touches nothing but process-local dicts, and swallows its
own errors. It runs AFTER the sync has committed, and it must never be able to fail a
sync write that already succeeded.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Events that change the CODE, so file/tree/hotspot/defect reads are now wrong.
_CODE_EVENTS = frozenset({"push", "create", "delete", "release", "repository"})

# Events that change the WORK (issues, PRs, reviews, labels) but not the tree. The
# planning payload embeds issue and PR listings, so it goes stale too; the tree does not.
_WORK_EVENTS = frozenset(
    {
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "milestone",
        "label",
    }
)


def repo_full_name_from_payload(payload: Any) -> str:
    """``owner/name`` from a webhook payload dict or a parsed model, or ""."""
    repo: Any = None
    if isinstance(payload, dict):
        repo = payload.get("repository")
    else:
        repo = getattr(payload, "repository", None)
    if isinstance(repo, dict):
        return str(repo.get("full_name") or "").strip()
    full = getattr(repo, "full_name", "")
    return str(full or "").strip()


def note_repo_changed(full_name: str, *, event: str = "") -> int:
    """Drop cached knowledge for a repo an event says has moved on.

    Returns the number of cache entries dropped, for logging and tests. Never raises:
    the caller is a webhook handler that has already committed real work.
    """
    name = (full_name or "").strip()
    if "/" not in name:
        return 0
    kind = (event or "").strip().casefold()
    if kind and kind not in _CODE_EVENTS and kind not in _WORK_EVENTS:
        return 0
    try:
        from boardman.github.read_cache import invalidate_repo

        dropped = invalidate_repo(name)
    except (
        Exception
    ):  # never let cache bookkeeping break a sync path  # noqa: BLE001 - observability failure must not affect the request
        logger.warning("cache invalidation failed for %s", name, exc_info=True)
        return 0
    if dropped:
        logger.info("repo changed (%s): dropped %d cached reads for %s", kind or "?", dropped, name)
    return dropped
