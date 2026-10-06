"""Map Boardman's workflow steps ("intents") to ClickUp status names and back.

ClickUp statuses are per list, so the names come from settings (CLICKUP_STATUS_*). An intent
whose setting is empty has no status: Boardman then leaves the status alone instead of guessing.
"""

from __future__ import annotations

from boardman.settings import settings

# Order matters for the reverse lookup: when two intents share a name (a fresh task and an
# assigned one are both "to do" by default) the earlier intent wins.
_INTENT_SETTINGS: tuple[tuple[str, str], ...] = (
    ("workflow_needs_assigned", "clickup_status_needs_assigned"),
    ("workflow_assigned", "clickup_status_assigned"),
    ("workflow_in_progress", "clickup_status_in_progress"),
    ("workflow_paused", "clickup_status_paused"),
    ("workflow_needs_qa", "clickup_status_needs_qa"),
    ("workflow_needs_qa_again", "clickup_status_needs_qa"),
    ("workflow_in_qa", "clickup_status_in_qa"),
    ("github_pr_review_approved", "clickup_status_approved"),
    ("workflow_completed", "clickup_status_completed"),
)


def status_for_intent(intent: str) -> str:
    """The ClickUp status name for ``intent``, or "" when none is configured."""
    for name, setting in _INTENT_SETTINGS:
        if name == intent:
            return (getattr(settings, setting, "") or "").strip()
    return ""


def intent_for_status(status: str) -> str:
    """The workflow intent a ClickUp status name stands for, or "" when it is unknown."""
    wanted = (status or "").strip().casefold()
    if not wanted:
        return ""
    for name, setting in _INTENT_SETTINGS:
        if (getattr(settings, setting, "") or "").strip().casefold() == wanted:
            return name
    return ""
