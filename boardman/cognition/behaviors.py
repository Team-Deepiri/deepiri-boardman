"""Seeded behavior specs for the cognition engine.

Each entry is a (behavior_key, description, expected_present, evidence) tuple that the
intent-vs-reality engine checks deterministically. Follows the DEFECT_PROBES table style.
"""

from __future__ import annotations

from boardman.cognition.evidence import BehaviorSpec, Evidence

BEHAVIORS: tuple[BehaviorSpec, ...] = (
    BehaviorSpec(
        behavior_key="pr_author_becomes_developer",
        description="Opening a PR assigns the author as the developer on the linked task",
        expected_present=(
            "boardman/services/pr_handler.py:handle_pr_opened",
            "boardman/services/sync_state.py:resolve_pr_state",
        ),
        evidence=(
            Evidence(
                kind="intent",
                subject="pr_author_becomes_developer",
                value="PR author is written as the task developer on open",
                source_type="code",
                source_ref="boardman/services/pr_handler.py:handle_pr_opened",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    ),
    BehaviorSpec(
        behavior_key="qa_assigned_at_pr_ready",
        description="QA engineer is picked and assigned when a PR moves to ready-for-review",
        expected_present=(
            "boardman/assignment/qa_picker.py",
            "boardman/services/pr_handler.py:handle_pr_opened",
        ),
        evidence=(
            Evidence(
                kind="intent",
                subject="qa_assigned_at_pr_ready",
                value="QA picker algorithm selects reviewer when PR is non-draft",
                source_type="code",
                source_ref="boardman/assignment/qa_picker.py",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    ),
    BehaviorSpec(
        behavior_key="pr_edit_links_late_issue_reference",
        description="Editing a PR to add Fixes #N links the PR to the existing task",
        expected_present=(
            "boardman/services/pr_handler.py:reconcile_pr_issue_links",
            "tests/test_pr_edited_relink.py",
        ),
        evidence=(
            Evidence(
                kind="fact",
                subject="pr_edit_links_late_issue_reference",
                value="reconcile_pr_issue_links re-runs issue-linking on pull_request.edited",
                source_type="code",
                source_ref="boardman/services/pr_handler.py:1504-1521",
                computed_at="2026-08-24T00:00:00Z",
            ),
            Evidence(
                kind="observed",
                subject="pr_edit_links_late_issue_reference",
                value="six test shapes pin this behavior (gained, unchanged, moved, replayed, URL, branch)",
                source_type="test",
                source_ref="tests/test_pr_edited_relink.py",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    ),
    BehaviorSpec(
        behavior_key="issue_opened_creates_task",
        description="A new GitHub issue creates a corresponding Plaky task",
        expected_present=(
            "boardman/services/issue_handler.py:handle_issue_opened",
            "boardman/services/sync_state.py:resolve_issue_state",
        ),
        evidence=(
            Evidence(
                kind="intent",
                subject="issue_opened_creates_task",
                value="handle_issue_opened files a Plaky task for every new issue",
                source_type="code",
                source_ref="boardman/services/issue_handler.py:handle_issue_opened",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    ),
    BehaviorSpec(
        behavior_key="reconcile_repairs_drift",
        description="The reconciliation loop detects and repairs GitHub-Plaky drift",
        expected_present=(
            "boardman/services/reconcile.py:reconcile_repo",
            "boardman/sqlite_worker.py",
        ),
        evidence=(
            Evidence(
                kind="fact",
                subject="reconcile_repairs_drift",
                value="reconcile_repo replays missed webhooks through the same idempotent handlers",
                source_type="code",
                source_ref="boardman/services/reconcile.py:reconcile_repo",
                computed_at="2026-08-24T00:00:00Z",
            ),
        ),
    ),
)
