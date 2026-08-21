"""Canonical, deterministic GitHub -> Plaky state resolution.

Webhook handlers, reconciliation, and the optional polling safety net should all
derive the same task metadata from the GitHub payload.  This module deliberately
contains no network calls and no Plaky writes; it is the state-resolution layer
between an event and a mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boardman.github.pr_signals import infer_task_type_from_pr, pr_label_names
from boardman.services.priority_rules import infer_priority_from_text, priority_from_github_label


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _login(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or "").strip()
    return str(value or "").strip()


def first_login(rows: Any) -> str:
    for row in rows or []:
        login = _login(row)
        if login:
            return login
    return ""


@dataclass(frozen=True, slots=True)
class GitHubSyncState:
    entity: str
    repo_full_name: str
    repo_name: str
    number: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    native_type: str
    task_type: str
    priority: str
    # True when a human set the priority ON GITHUB (sidebar Priority field or a
    # priority label). Only then may later syncs overwrite the board's value —
    # inferred priorities must not stomp a lead's hand-tuning on unrelated events.
    priority_explicit: bool
    assignee_login: str
    author_login: str
    state: str
    merged: bool
    draft: bool
    head_ref: str


def _labels(value: Any) -> tuple[str, ...]:
    return tuple(pr_label_names(value))


def _native_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def issue_field_priority(issue: Any) -> str:
    """Priority from GitHub's sidebar issue fields (org feature, 2025+).

    The REST payload carries `issue_field_values` rows like
    {"issue_field_name": "Priority", "data_type": "single_select",
     "single_select_option": {"name": "High"}}. This is where the team actually
    sets priority (verified live on issue #87: sidebar said High, no label, no
    project — and the task sat on the inferred Medium).
    """
    for row in _value(issue, "issue_field_values", []) or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("issue_field_name") or "").strip().casefold() != "priority":
            continue
        opt = row.get("single_select_option")
        name = opt.get("name") if isinstance(opt, dict) else None
        mapped = priority_from_github_label(name)
        if mapped:
            return mapped
    return ""


def resolve_issue_state(issue: Any, *, repo_full_name: str, repo_name: str) -> GitHubSyncState:
    labels = _labels(_value(issue, "labels", []))
    native = _native_type(_value(issue, "type"))
    title = str(_value(issue, "title", "") or "").strip()
    body = str(_value(issue, "body", "") or "")
    assignees = _value(issue, "assignees", []) or []
    single_assignee = _value(issue, "assignee")
    if single_assignee and not assignees:
        assignees = [single_assignee]
    explicit_priority = (
        issue_field_priority(issue)
        # GitHub Projects (org-level, 2024+) can set a top-level "priority" object
        # on issues when the Priority field is configured. This is not a label; it is
        # the sidebar Priority field, which has name/id/color like the Type field.
        or priority_from_github_label(_value(issue, "priority"))
        or next((p for p in (priority_from_github_label(lb) for lb in labels) if p), "")
    )
    return GitHubSyncState(
        entity="issue",
        repo_full_name=repo_full_name,
        repo_name=repo_name,
        number=int(_value(issue, "number", 0) or 0),
        title=title,
        body=body,
        url=str(_value(issue, "html_url", "") or "").strip(),
        labels=labels,
        native_type=native,
        task_type=native or infer_task_type_from_pr(None, labels) or "Feature",
        priority=explicit_priority or infer_priority_from_text(title, body, labels),
        priority_explicit=bool(explicit_priority),
        assignee_login=first_login(assignees),
        author_login=_login(_value(issue, "user")),
        state=str(_value(issue, "state", "open") or "open").strip().casefold(),
        merged=False,
        draft=False,
        head_ref="",
    )


def resolve_pr_state(pr: Any, *, repo_full_name: str, repo_name: str) -> GitHubSyncState:
    labels = _labels(_value(pr, "labels", []))
    head = _value(pr, "head", {}) or {}
    head_ref = str(_value(head, "ref", "") or "").strip()
    user = _value(pr, "user", {}) or {}
    assignee = first_login(_value(pr, "assignees", []) or [])
    # A PR author owns the work when GitHub has no explicit PR assignee.  This
    # is the canonical initial-owner rule used both on open and later edits.
    author = _login(user)
    explicit_priority = next(
        (p for p in (priority_from_github_label(lb) for lb in labels) if p), ""
    )
    return GitHubSyncState(
        entity="pull_request",
        repo_full_name=repo_full_name,
        repo_name=repo_name,
        number=int(_value(pr, "number", 0) or 0),
        title=str(_value(pr, "title", "") or "").strip(),
        body=str(_value(pr, "body", "") or ""),
        url=str(_value(pr, "html_url", "") or "").strip(),
        labels=labels,
        native_type="",
        task_type=infer_task_type_from_pr(head_ref, labels) or "Feature",
        priority=explicit_priority
        or infer_priority_from_text(
            str(_value(pr, "title", "") or ""), str(_value(pr, "body", "") or ""), labels
        ),
        # Only a priority LABEL on the PR is a statement of priority. Wording guessed
        # from a PR title is not, and writing it downgraded issues that had been marked
        # Urgent by hand: the PR that fixed them reset the column to Medium.
        priority_explicit=bool(explicit_priority),
        assignee_login=assignee or author,
        author_login=author,
        state=str(_value(pr, "state", "open") or "open").strip().casefold(),
        merged=bool(_value(pr, "merged", False)),
        draft=bool(_value(pr, "draft", False)),
        head_ref=head_ref,
    )


def issue_status_intent(state: GitHubSyncState, *, engineer_plaky_id: str | None = None) -> str:
    """The one rule for what an issue's status should be.

    Claiming "Assigned" requires a person who will actually land in the Assignee
    column. A GitHub assignee that resolves to nobody on the board would otherwise
    write Assigned onto a task with an empty Assignee — the state the board rules
    forbid. ``engineer_plaky_id`` is a three-way signal: None means the caller has not
    resolved anyone yet (fall back to the GitHub login), "" means it resolved and found
    nobody, and an id means that person is being written.
    """
    if state.state == "closed":
        return "workflow_completed"
    owner = state.assignee_login if engineer_plaky_id is None else engineer_plaky_id.strip()
    return "workflow_assigned" if owner else "workflow_needs_assigned"


def pr_label_task_type(labels: Any) -> str:
    """Resolve a PR label change without letting its original branch win forever."""
    return infer_task_type_from_pr(None, pr_label_names(labels)) or "Feature"


# --- Workflow ordering -----------------------------------------------------------------
#
# How far along a task is, as a number. Only the ORDER matters; the gaps are meaningless.
#
# This exists because "who owns this issue" and "where has the work got to" are different
# questions, and only the second one is a workflow position. A GitHub `assigned` event
# answers the first: it says a developer is on it, which is true whether QA is halfway
# through reviewing the PR or nobody has started. Writing the assignee-derived status
# unconditionally answered the second question with the first question's answer, so
# assigning a second developer to an issue whose PR was already In QA moved the board
# back to Assigned and QA's work vanished from view.
#
# Paused is deliberately ranked with In Progress rather than below it: pausing is a
# statement about the work continuing later, not a demotion to unowned.
WORKFLOW_RANK: dict[str, int] = {
    "workflow_needs_assigned": 0,
    "workflow_assigned": 1,
    "workflow_in_progress": 2,
    "workflow_paused": 2,
    "workflow_needs_qa": 3,
    "workflow_needs_qa_again": 3,
    "github_pr_review_changes_requested": 3,
    "workflow_in_qa": 4,
    "github_pr_review_approved": 5,
    "workflow_completed": 6,
}

# The intents derived purely from "does someone own this", which are the ones that must
# never overwrite a further-along status. Every other intent is a deliberate workflow
# transition -- QA rejecting, a merge completing -- and those are allowed to move a task
# in either direction, because that is what they are for.
ASSIGNEE_DERIVED_INTENTS = frozenset({"workflow_needs_assigned", "workflow_assigned"})


def workflow_rank(intent: str) -> int | None:
    """How far along `intent` sits, or None when it is not a workflow position."""
    return WORKFLOW_RANK.get((intent or "").strip())


def status_intent_would_regress(current_intent: str, next_intent: str) -> bool:
    """True when writing `next_intent` would move a task BACKWARDS through the workflow.

    Only consulted for assignee-derived intents. An unknown current status returns False:
    a board using labels this code cannot place is not evidence of anything, and refusing
    to write on that basis would silently stop syncing rather than risk a regression.
    """
    if next_intent not in ASSIGNEE_DERIVED_INTENTS:
        return False
    current = workflow_rank(current_intent)
    following = workflow_rank(next_intent)
    if current is None or following is None:
        return False
    return following < current
