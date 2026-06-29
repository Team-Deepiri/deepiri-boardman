"""Tests for PR link comment helpers."""

import boardman.settings as boardman_settings
from boardman.services.pr_link_comment import (
    collect_pr_urls,
    format_pr_link_comment,
    format_pr_notice_with_url,
)


def test_collect_pr_urls_dedupes_and_orders():
    assert collect_pr_urls(
        pr_url="https://a.com/1",
        pr_urls=["https://b.com/2", "https://a.com/1"],
    ) == ["https://a.com/1", "https://b.com/2"]


def test_format_pr_link_comment_single_html(monkeypatch):
    monkeypatch.setattr(boardman_settings.settings, "plaky_pr_comment_links_as_html", True)
    t = format_pr_link_comment(["https://github.com/o/r/pull/1"])
    assert "PR linked:" in t
    assert '<a href="https://github.com/o/r/pull/1">' in t
    assert "PR #1" in t


def test_format_pr_link_comment_multi_html(monkeypatch):
    monkeypatch.setattr(boardman_settings.settings, "plaky_pr_comment_links_as_html", True)
    t = format_pr_link_comment(
        ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"],
    )
    assert "PRs linked:" in t
    assert t.count("<a ") == 2
    assert '<a href="https://github.com/o/r/pull/1">' in t


def test_format_pr_link_comment_plain_when_disabled(monkeypatch):
    monkeypatch.setattr(boardman_settings.settings, "plaky_pr_comment_links_as_html", False)
    t = format_pr_link_comment(["https://github.com/o/r/pull/9"])
    assert t == "PR linked:\nhttps://github.com/o/r/pull/9"
    m = format_pr_link_comment(
        ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"],
    )
    assert "https://github.com/o/r/pull/1" in m
    assert "https://github.com/o/r/pull/2" in m
    assert "<a " not in m


def test_format_pr_notice_with_url_html(monkeypatch):
    monkeypatch.setattr(boardman_settings.settings, "plaky_pr_comment_links_as_html", True)
    s = format_pr_notice_with_url(
        headline="**PR Opened:**", pr_number=13, pr_url="https://g/x/y/pull/13"
    )
    assert "**PR Opened:**" in s
    assert '<a href="https://g/x/y/pull/13">' in s
    assert "#13" in s
