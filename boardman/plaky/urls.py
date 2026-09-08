"""Plaky web URL helpers — turn a stored task id/url into a clickable web link.

The Plaky public API rarely returns a web URL for a board item; the creation
response's ``url`` / ``taskUrl`` is often ``None``. Plaky's own web app has no
top-level ``/task/{id}`` or ``/i/{id}`` route — its router (see the bundled
``main-*.js``) only resolves item links nested under a space and board:
``/spaces/{spaceId}/boards/{boardId}/views/{viewId}/items/{itemId}``, and the
app itself falls back to ``views/0`` when no specific view is known. A bare
``/task/{id}`` link 404s ("This Page Isn't Available") — this module builds
the real nested route, falling back to the old (broken) format only when the
board/space ids aren't known at the call site.
"""

from __future__ import annotations

PLAKY_APP_BASE = "https://app.plaky.com"
_DEFAULT_VIEW_ID = "0"


def plaky_task_web_url(
    task_id: str,
    task_url_hint: str | None = None,
    *,
    board_id: str | None = None,
    space_id: str | None = None,
    view_id: str | None = None,
) -> str:
    """Return the best web URL for a Plaky item.

    - If ``task_url_hint`` is an http(s) URL *and* it actually points at
      ``task_id`` (the id appears in the URL path), trust it verbatim (it
      came from the API or from ``IssueTaskMap.plaky_task_url``).
    - Otherwise, if ``board_id`` and ``space_id`` are known, build the real
      nested route Plaky's own app uses:
      ``https://app.plaky.com/spaces/{space_id}/boards/{board_id}/views/{view_id}/items/{task_id}``.
    - Otherwise fall back to the legacy ``https://app.plaky.com/task/{id}``
      guess. That format is not a real route in Plaky's router and will 404 —
      callers should pass ``board_id``/``space_id`` whenever available.
    - ``pending:*`` ids are not real items and return the hint (usually empty).

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

    bid = (board_id or "").strip()
    sid = (space_id or "").strip()
    if bid and sid:
        vid = (view_id or "").strip() or _DEFAULT_VIEW_ID
        return f"{PLAKY_APP_BASE}/spaces/{sid}/boards/{bid}/views/{vid}/items/{tid}"

    # Legacy guess kept only for call sites that don't yet have board/space ids
    # plumbed through. Not a real Plaky route — 404s in the web app.
    return f"{PLAKY_APP_BASE}/task/{tid}"


def plaky_task_markdown_link(
    task_id: str,
    task_url_hint: str | None = None,
    *,
    board_id: str | None = None,
    space_id: str | None = None,
    view_id: str | None = None,
) -> str:
    """Markdown link for a Plaky task: ``[Plaky task 7332088](https://...)``.

    Falls back to backticked ``Plaky task `id``` when no URL can be synthesized
    (e.g. a ``pending:*`` reservation before the item exists).
    """
    tid = (task_id or "").strip()
    url = plaky_task_web_url(
        tid, task_url_hint, board_id=board_id, space_id=space_id, view_id=view_id
    )
    if url.startswith("http"):
        return f"[Plaky task {tid}]({url})"
    if tid:
        return f"Plaky task `{tid}`"
    return "Plaky task"
