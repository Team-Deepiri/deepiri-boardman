"""Small deterministic assistant intents that do not need an LLM round trip."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from boardman.plaky.client import PlakyClient
from boardman.repos_config import get_routing
from boardman.settings import settings


@dataclass(frozen=True)
class FastPathResult:
    reply: str
    intent: str


_CURRENT_REPO = re.compile(
    r"\b(?:what|which)\s+(?:is\s+)?(?:my\s+)?(?:current\s+)?(?:repo|repository|project)\b"
    r"|\bwhat\s+repo\s+am\s+i\s+(?:currently\s+)?working\s+with\b",
    re.IGNORECASE,
)
_ROUTING = re.compile(
    r"\b(?:where|which|what)\b.{0,80}\b(?:board|group|table|route|routing)\b"
    r"|\b(?:board|group|table|route|routing)\b.{0,80}\b(?:repo|task|project)\b",
    re.IGNORECASE,
)
_LIST_TASKS = re.compile(
    r"^\s*(?:list|show|display|what\s+are)\b.*\b(?:open|active|current)?\s*"
    r"(?:plaky\s+)?(?:tasks?|items?)\b",
    re.IGNORECASE,
)
# "What's on the board right now?" is a question about the WORK, not about where work
# gets filed - but it says "what" and "board", so the routing pattern above swallowed it
# and answered with two ids. Anything asking what is on / in / sitting on a board, or
# what a board looks like, belongs to the reasoning path that can actually read it.
_BOARD_CONTENT = re.compile(
    r"\b(?:what|what's|whats|anything|everything|how\s+many|who)\b[^?]{0,40}?"
    r"\b(?:on|in|inside|left\s+on|sitting\s+on)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:plaky\s+)?(?:board|group|table)\b"
    r"|\b(?:board|group|table)\s+(?:right\s+now|currently|today|look\s+like|status)\b",
    re.IGNORECASE,
)
# ...unless the question is plainly about WHERE work is filed, in which case the routing
# answer is the right one even though it says "in the board".
_ROUTING_VERB = re.compile(
    r"\b(?:route[sd]?|routing|go(?:es)?\s+(?:in|into|to)|belongs?|put|file[sd]?|land)\b",
    re.IGNORECASE,
)
_WRITE_WORD = re.compile(r"\b(?:create|make|add|update|move|close|delete|archive|assign)\b", re.I)


def _task_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("tasks", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _format_task_list(payload: dict[str, Any], repo: str | None) -> str:
    if not payload.get("ok"):
        message = str(payload.get("message") or payload.get("error") or "Plaky returned an error")
        return f"I couldn't read open Plaky tasks: {message}"
    rows = _task_rows(payload)
    scope = f" for `{repo}`" if repo else ""
    if not rows:
        return f"There are no open Plaky tasks{scope}."
    lines = [f"Open Plaky tasks{scope} ({len(rows)}):"]
    for row in rows[:30]:
        task_id = row.get("id") or row.get("taskId") or row.get("item_id") or "?"
        title = row.get("title") or row.get("name") or "Untitled task"
        status = row.get("status") or row.get("state") or ""
        suffix = f" — {status}" if status else ""
        lines.append(f"- `{task_id}` {str(title).strip()[:180]}{suffix}")
    if len(rows) > 30:
        lines.append(f"- …and {len(rows) - 30} more")
    return "\n".join(lines)


async def maybe_fast_path(
    message: str,
    *,
    repo: str | None,
    board_id: str | None,
    group_id: str | None,
) -> FastPathResult | None:
    """Handle only unambiguous, read-only intents; return None for normal reasoning."""

    text = (message or "").strip()
    if not text:
        return None

    if _CURRENT_REPO.search(text):
        if repo:
            return FastPathResult(f"You’re currently working with `{repo}`.", "current_repo")
        return FastPathResult("There is no active repository in this session.", "current_repo")

    asks_for_content = bool(_BOARD_CONTENT.search(text)) and not _ROUTING_VERB.search(text)
    if repo and _ROUTING.search(text) and not asks_for_content:
        short = repo.rsplit("/", 1)[-1]
        routing = get_routing(repo, short, settings.github_org)
        if routing:
            bid = (board_id or routing.plaky_board_id or "").strip() or "not resolved"
            gid = (group_id or routing.plaky_group_id or "").strip() or "not resolved"
            label = (routing.plaky_table or "").strip()
            label_text = f" ({label})" if label else ""
            return FastPathResult(
                f"`{repo}` routes to Plaky board `{bid}` and group `{gid}`{label_text}.",
                "repo_routing",
            )

    if _LIST_TASKS.match(text) and not _WRITE_WORD.search(text) and board_id:
        payload = await PlakyClient().get_tasks(status="open", board_id=board_id)
        return FastPathResult(_format_task_list(payload, repo), "list_open_tasks")

    return None
