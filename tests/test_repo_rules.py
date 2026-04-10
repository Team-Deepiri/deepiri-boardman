"""QA tier 1/2/3 vs repo patterns."""

from boardman.assignment.repo_rules import (
    default_qa_repo_rules,
    qa_tier_allows_repo,
    repo_matches_any_pattern,
)


def test_tier3_always_allows():
    rules = default_qa_repo_rules()
    assert qa_tier_allows_repo(3, "deepiri-org/boardman", rules) is True


def test_tier2_blocked_on_boardman():
    rules = default_qa_repo_rules()
    assert qa_tier_allows_repo(2, "deepiri-org/deepiri-boardman", rules) is False
    assert qa_tier_allows_repo(3, "deepiri-org/deepiri-boardman", rules) is True


def test_tier1_only_core_repos():
    rules = default_qa_repo_rules()
    assert qa_tier_allows_repo(1, "deepiri-org/deepiriweb-frontend", rules) is True
    assert qa_tier_allows_repo(1, "deepiri-org/boardman", rules) is False


def test_repo_matches_any_pattern():
    assert repo_matches_any_pattern("Deepiri-Org/BoardMan", ["*boardman*"]) is True
