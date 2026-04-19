"""QA tier team slug/name parsing (no GitHub calls)."""

from boardman.github.qa_tier_teams import parse_qa_tier_from_github_team


def test_parse_qa_tier_standard_patterns():
    assert parse_qa_tier_from_github_team("qa-tier-1", None) == 1
    assert parse_qa_tier_from_github_team("qa-tier-2", "Display") == 2
    assert parse_qa_tier_from_github_team("whatever", "QA Tier 3 Reviewers") == 3
    assert parse_qa_tier_from_github_team("tier-3-qa", None) == 3


def test_parse_qa_tier_t_qa_slug():
    assert parse_qa_tier_from_github_team("t2-qa", None) == 2
    assert parse_qa_tier_from_github_team("acme-t3-qa-extra", None) == 3


def test_parse_qa_tier_qa_t_slug():
    assert parse_qa_tier_from_github_team("qa-t1", None) == 1


def test_parse_qa_tier_level():
    assert parse_qa_tier_from_github_team("level-2-qa", None) == 2


def test_support_like_slug_no_false_positive():
    assert parse_qa_tier_from_github_team("support-team", None) is None
    assert parse_qa_tier_from_github_team("platform-api", None) is None
