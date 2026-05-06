"""Format Plaky comments when linking GitHub PR(s) to a task."""

from __future__ import annotations

from html import escape

from boardman.settings import settings


def collect_pr_urls(*, pr_url: str | None, pr_urls: list[str] | None) -> list[str]:
    """Merge optional singular and list PR URLs, strip, dedupe (order preserved)."""
    raw: list[str] = []
    u = (pr_url or "").strip()
    if u:
        raw.append(u)
    if pr_urls:
        for x in pr_urls:
            s = str(x or "").strip()
            if s:
                raw.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _pr_anchor_label(url: str) -> str:
    """Short label from a GitHub PR URL (e.g. PR #13), else fall back to the URL."""
    u = (url or "").strip().rstrip("/")
    if "/pull/" in u.lower():
        tail = u.split("/pull/", 1)[-1].split("/")[0].split("?")[0]
        repo = u.split("/pull/")[0].split("/")[-1]
        if tail.isdigit():
            return f"{repo} PR #{tail}"
    return u


def format_pr_link_comment(urls: list[str]) -> str:
    """HTML comment for one or more PR links."""
    if not urls:
        return ""
    cleaned = [u.strip() for u in urls if (u or "").strip()]
    if not cleaned:
        return ""

    if settings.plaky_pr_comment_links_as_html:
        sep = "<br/>"
        anchors = []
        for u in cleaned:
            href = escape(u, quote=True)
            label = escape(_pr_anchor_label(u))
            anchors.append(f'<a href="{href}">{label}</a>')
        joined = sep.join(anchors)
        if len(cleaned) == 1:
            return f"PR linked: {joined}"
        return f"PRs linked:{sep}{joined}"

    if len(cleaned) == 1:
        return f"PR linked:\n{cleaned[0]}"
    return "PRs linked:\n\n" + "\n\n".join(cleaned)


def format_pr_notice_with_url(*, headline: str, pr_number: int | None, pr_url: str) -> str:
    """GitHub webhook–style lines; same HTML vs plain policy as ``format_pr_link_comment``."""
    url = (pr_url or "").strip()
    if not url:
        return headline
    if settings.plaky_pr_comment_links_as_html:
        href = escape(url, quote=True)
        inner = escape(f"#{pr_number}") if pr_number is not None else escape(_pr_anchor_label(url))
        return f"{headline} <a href=\"{href}\">{inner}</a>"
    num_line = f"#{pr_number}" if pr_number is not None else _pr_anchor_label(url)
    return f"{headline} {num_line}\n{url}"
