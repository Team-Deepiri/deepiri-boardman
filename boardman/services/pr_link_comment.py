"""Format Plaky comments when linking GitHub PR(s) to a task."""

from __future__ import annotations


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


def format_pr_link_comment(urls: list[str]) -> str:
    """Markdown comment for one or more PR links."""
    if not urls:
        return ""
    if len(urls) == 1:
        return f"**PR linked:** [View PR]({urls[0]})"
    lines = "\n".join(f"- [View PR]({u})" for u in urls)
    return f"**PRs linked:**\n{lines}"
