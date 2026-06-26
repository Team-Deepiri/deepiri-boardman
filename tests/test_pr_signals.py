"""Pure PR signal extraction: type-from-branch/labels, pause, QA @-mention."""

from __future__ import annotations

import pytest

from boardman.github.pr_signals import (
    comment_mentions_qa_or_support,
    comment_requests_pause,
    infer_task_type_from_pr,
    pr_label_names,
)


@pytest.mark.parametrize(
    "head_ref, expected",
    [
        ("feat/new-login", "Feature"),
        ("feature/123-add-thing", "Feature"),
        ("fix/crash-on-start", "Bug"),
        ("bugfix/null-deref", "Bug"),
        ("hotfix/prod-down", "Bug"),
        ("docs/readme", "Documentation"),
        ("chore/bump-deps", "Chore"),
        ("refactor/extract-service", "Refactoring"),
        ("test/add-coverage", "Tests"),
        ("refs/heads/fix/redirect", "Bug"),
        ("feat-123-no-slash", "Feature"),
        ("random-branch-name", ""),
        ("", ""),
    ],
)
def test_infer_type_from_branch(head_ref, expected):
    assert infer_task_type_from_pr(head_ref, None) == expected


def test_branch_beats_labels():
    assert infer_task_type_from_pr("fix/x", ["feature"]) == "Bug"


def test_label_fallback_when_branch_has_no_convention():
    assert infer_task_type_from_pr("my-branch", ["bug"]) == "Bug"
    assert infer_task_type_from_pr("my-branch", ["type: feature"]) == "Feature"
    assert infer_task_type_from_pr("my-branch", ["kind/documentation"]) == "Documentation"


def test_label_no_match_returns_empty():
    assert infer_task_type_from_pr("my-branch", ["needs-review", "p1"]) == ""


def test_pr_label_names_from_dicts_and_strings():
    labels = [{"name": "bug"}, {"name": " feature "}, "chore", {"nope": 1}, ""]
    assert pr_label_names(labels) == ["bug", "feature", "chore"]


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Let's pause this for now", True),
        ("Paused until next sprint", True),
        ("Putting this on hold", True),
        ("on-hold pending design", True),
        ("Looks good, merging", False),
        ("This unpauses nothing", False),  # 'unpauses' is not a word-boundaried 'pause'
        ("", False),
        (None, False),
    ],
)
def test_comment_requests_pause(body, expected):
    assert comment_requests_pause(body) is expected


def test_mention_of_support_login():
    support = {"qa-lead", "alice-qa"}
    assert comment_mentions_qa_or_support("hey @qa-lead can you review", support) is True
    assert comment_mentions_qa_or_support("ping @Alice-QA please", support) is True


def test_mention_team_handle():
    assert comment_mentions_qa_or_support("@Team-Deepiri/support-team take a look", set()) is True
    assert comment_mentions_qa_or_support("ready @qa", set()) is True


def test_no_mention_or_unrelated_mention():
    support = {"qa-lead"}
    assert comment_mentions_qa_or_support("just @bob fyi", support) is False
    assert comment_mentions_qa_or_support("no mentions here", support) is False
    assert comment_mentions_qa_or_support("", support) is False
