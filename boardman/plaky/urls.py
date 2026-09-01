"""Plaky web URL helpers — turn a stored task id/url into a clickable web link.

The Plaky public API rarely returns a web URL for a board item; the creation
response's ``url`` / ``taskUrl`` is often ``None``. Boardman already synthesizes
``https://app.plaky.com/task/{id}`` in the legacy ``/tasks`` path — this module
centralises that and the markdown rendering so the QA-assignment comment
(and any future surface) always shows a real, clickable link instead of a bare
``Plaky task `7332088```.
"""

from __future__ import annotations


def plaky_task_web_url(task_id: str, task_url_hint: str | None = None) -> str:
    """Return the best web URL for a Plaky item.

    - If ``task_url_hint`` is an http(s) URL *and* it actually points at
      ``task_id`` (the id appears in the URL path), trust it verbatim (it
      came from the API or from ``IssueTaskMap.plaky_task_url``).
    - Otherwise synthesize the universal deep-link ``https://app.plaky.com/task/{id}``
      which Plaky resolves to the concrete board/item page. ``pending:*`` ids are
      not real items and return the hint (usually empty).

    A stored hint for a stale/relinked task can point at a *different* item
    than ``task_id`` now refers to (e.g. after a task got reassigned or the
    mapping row was relinked). Rendering that hint verbatim produces a link
    whose visible id doesn't match where it actually goes — this check
    catches that mismatch and falls back to the id-derived link instead.
    """
    hint = (task_url_hint or "").strip()
    tid = (task_id or "").strip()
    if (hint.startswith("http://") or hint.startswith("https://")) and (not tid or tid in hint):
        return hint
    if not tid or tid.startswith("pending:"):
        return hint
    # ``app.plaky.com/task/{id}`` is the documented fallback already used in
    # boardman/plaky/client.py (legacy path) and boardman/agent/tools/plaky_tools.py.
    # It works regardless of board/space; board-specific ``/board/{bid}/item/{id}``
    # links are not discoverable without extra API calls and break if the item moves.
    return f"https://app.plaky.com/task/{tid}"


def plaky_task_markdown_link(task_id: str, task_url_hint: str | None = None) -> str:
    """Markdown link for a Plaky task: ``[Plaky task 7332088](https://...)``.

    Falls back to backticked ``Plaky task `id``` when no URL can be synthesized
    (e.g. a ``pending:*`` reservation before the item exists).
    """
    tid = (task_id or "").strip()
    url = plaky_task_web_url(tid, task_url_hint)
    if url.startswith("http"):
        return f"[Plaky task {tid}]({url})"
    if tid:
        return f"Plaky task `{tid}`"
    return "Plaky task"
