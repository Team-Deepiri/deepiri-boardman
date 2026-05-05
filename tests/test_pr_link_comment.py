"""Tests for PR link comment helpers."""

from boardman.services.pr_link_comment import collect_pr_urls, format_pr_link_comment


def test_collect_pr_urls_dedupes_and_orders():
    assert collect_pr_urls(
        pr_url="https://a.com/1",
        pr_urls=["https://b.com/2", "https://a.com/1"],
    ) == ["https://a.com/1", "https://b.com/2"]


def test_format_pr_link_comment_single():
    t = format_pr_link_comment(["https://github.com/o/r/pull/1"])
    assert "PR linked" in t
    assert "https://github.com/o/r/pull/1" in t


def test_format_pr_link_comment_multi():
    t = format_pr_link_comment(
        ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"],
    )
    assert "PRs linked" in t
    assert t.count("View PR") == 2
