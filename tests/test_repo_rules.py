"""QA tier 1/2/3 vs repo patterns."""

from boardman.assignment.repo_rules import (
    QaRepoRules,
    default_qa_repo_rules,
    qa_tier_allows_repo,
    repo_matches_any_pattern,
)


def test_tier3_always_allows():
    rules = default_qa_repo_rules()
    assert qa_tier_allows_repo(3, "deepiri-org/boardman", rules) is True


def test_tier2_blocked_on_boardman():
    """Explicit patterns (defaults from YAML are empty until configured)."""
    rules = QaRepoRules(tier2_excluded_patterns=["*boardman*"], tier1_only_patterns=[])
    assert qa_tier_allows_repo(2, "deepiri-org/deepiri-boardman", rules) is False
    assert qa_tier_allows_repo(3, "deepiri-org/deepiri-boardman", rules) is True


def test_tier1_only_core_repos():
    rules = QaRepoRules(
        tier2_excluded_patterns=[],
        tier1_only_patterns=["*deepiriweb-frontend*", "*frontend*"],
    )
    assert qa_tier_allows_repo(1, "deepiri-org/deepiriweb-frontend", rules) is True
    assert qa_tier_allows_repo(1, "deepiri-org/boardman", rules) is False


def test_repo_matches_any_pattern():
    assert repo_matches_any_pattern("Deepiri-Org/BoardMan", ["*boardman*"]) is True


def test_fractional_qa_tier_is_floored_not_rounded():
    """A person at 2.9 is genuinely not yet proven at tier 3 -- flooring (not
    rounding) must not grant tier-3 access on the strength of rounding alone."""
    rules = QaRepoRules(tier2_excluded_patterns=["*boardman*"], tier1_only_patterns=[])
    assert qa_tier_allows_repo(2.9, "deepiri-org/deepiri-boardman", rules) is False
    assert qa_tier_allows_repo(3.0, "deepiri-org/deepiri-boardman", rules) is True


def test_fractional_qa_tier_below_two_floors_to_tier1_rules():
    rules = QaRepoRules(
        tier2_excluded_patterns=[],
        tier1_only_patterns=["*frontend*"],
    )
    assert qa_tier_allows_repo(1.9, "deepiri-org/deepiriweb-frontend", rules) is True
    assert qa_tier_allows_repo(1.9, "deepiri-org/boardman", rules) is False
