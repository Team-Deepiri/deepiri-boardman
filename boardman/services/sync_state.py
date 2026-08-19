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
from boardman.services.priority_rules import infer_priority_from_text


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


def resolve_issue_state(issue: Any, *, repo_full_name: str, repo_name: str) -> GitHubSyncState:
    labels = _labels(_value(issue, "labels", []))
    native = _native_type(_value(issue, "type"))
    title = str(_value(issue, "title", "") or "").strip()
    body = str(_value(issue, "body", "") or "")
    assignees = _value(issue, "assignees", []) or []
    single_assignee = _value(issue, "assignee")
    if single_assignee and not assignees:
        assignees = [single_assignee]
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
        priority=infer_priority_from_text(title, body, labels),
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
        priority=infer_priority_from_text(
            str(_value(pr, "title", "") or ""), str(_value(pr, "body", "") or ""), labels
        ),
        assignee_login=assignee or author,
        author_login=author,
        state=str(_value(pr, "state", "open") or "open").strip().casefold(),
        merged=bool(_value(pr, "merged", False)),
        draft=bool(_value(pr, "draft", False)),
        head_ref=head_ref,
    )


def issue_status_intent(state: GitHubSyncState) -> str:
    if state.state == "closed":
        return "workflow_completed"
    return "workflow_assigned" if state.assignee_login else "workflow_needs_assigned"


def pr_label_task_type(labels: Any) -> str:
    """Resolve a PR label change without letting its original branch win forever."""
    return infer_task_type_from_pr(None, pr_label_names(labels)) or "Feature"
