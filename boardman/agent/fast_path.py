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


_DEFAULT_BRANCH = re.compile(
    r"\b(?:default|main|base)\s+branch\b|\bwhat\s+branch\b|\bwhich\s+branch\b", re.IGNORECASE
)
# "which task is issue 91", "is there a task for issue 4242", "issue 90 - which card?".
# Both orders, because people write it both ways.
_ISSUE_NUMBER = re.compile(
    r"\b(?:issue|#)\s*#?(\d{1,6})\b.{0,40}\b(?:task|plaky|card|tracked|linked)\b"
    r"|\b(?:task|plaky|card)\b.{0,40}\b(?:for|of|from|is|linked\s+to)\s+"
    r"(?:issue\s*)?#?(\d{1,6})\b",
    re.IGNORECASE,
)
_HOW_MANY_ISSUES = re.compile(
    r"\bhow\s+many\b.{0,30}\bissues?\b|\bissue\s+count\b|\bcount\s+of\s+issues\b", re.IGNORECASE
)
_HOW_MANY_PRS = re.compile(
    r"\bhow\s+many\b.{0,30}\b(?:prs?|pull\s+requests?)\b|\bpr\s+count\b", re.IGNORECASE
)
_ASKS_LIVE = re.compile(
    r"\b(?:right\s+now|currently|live|check\s+github|as\s+of\s+now|latest)\b", re.IGNORECASE
)


def _answer_from_state(text: str, state: Any | None) -> FastPathResult | None:
    """Answer from project state, or None to let the reasoning path handle it.

    Only questions whose whole answer is a stored fact. Anything needing judgment,
    ranking, code, or a value that could have changed since the last webhook goes to the
    agent — the cost of being wrong here is a confident, instant falsehood.
    """
    if state is None or not getattr(getattr(state, "identity", None), "repo_full_name", ""):
        return None
    ident, live = state.identity, state.live

    if _DEFAULT_BRANCH.search(text) and ident.default_branch:
        return FastPathResult(
            f"`{ident.repo_full_name}` builds from `{ident.default_branch}`.",
            "default_branch",
        )

    # Counts come from the sync engine's own mapping tables, so they describe the BOARD.
    # "right now" means the caller wants GitHub checked, and that is not this path's job.
    if live.available and not _ASKS_LIVE.search(text):
        if _HOW_MANY_ISSUES.search(text):
            n = len(live.tracked_issues)
            if n:
                head = ", ".join(f"#{x}" for x in live.tracked_issues[:10])
                more = "" if n <= 10 else f" and {n - 10} more"
                return FastPathResult(
                    f"{n} issue{'s' if n != 1 else ''} from `{ident.repo_full_name}` are on the "
                    f"board: {head}{more}.",
                    "issue_count",
                )
            return FastPathResult(
                f"No issues from `{ident.repo_full_name}` are mapped to a Plaky task yet.",
                "issue_count",
            )

        if _HOW_MANY_PRS.search(text):
            n = len(live.active_prs)
            if n:
                rows = "\n".join(
                    f"- PR #{p.number} -> task `{p.task_id}`"
                    + (f" (issue #{p.issue_number})" if p.issue_number else "")
                    for p in live.active_prs[:10]
                )
                return FastPathResult(
                    f"{n} pull request{'s' if n != 1 else ''} on `{ident.repo_full_name}` are "
                    f"linked to a live task, and {live.merged_prs} have merged:\n{rows}",
                    "pr_count",
                )
            return FastPathResult(
                f"No open pull requests on `{ident.repo_full_name}` are linked to a task; "
                f"{live.merged_prs} have merged.",
                "pr_count",
            )

        hit = _ISSUE_NUMBER.search(text)
        if hit:
            number = int(hit.group(1) or hit.group(2))
            for pr in live.active_prs:
                if pr.issue_number == number:
                    return FastPathResult(
                        f"Issue #{number} is task `{pr.task_id}`, with PR #{pr.number} open "
                        f"against it.",
                        "issue_task_lookup",
                    )
            if number in live.tracked_issues:
                return FastPathResult(
                    f"Issue #{number} is tracked on board `{ident.board_id}`.",
                    "issue_task_lookup",
                )
            # Absence is a real answer here: the mapping table is the source of truth for
            # whether an issue was ever synced.
            return FastPathResult(
                f"Issue #{number} has no Plaky task mapped in `{ident.repo_full_name}`.",
                "issue_task_lookup",
            )
    return None


async def maybe_fast_path(
    message: str,
    *,
    repo: str | None,
    board_id: str | None,
    group_id: str | None,
    state: Any | None = None,
) -> FastPathResult | None:
    """Handle only unambiguous, read-only intents; return None for normal reasoning.

    ``state`` is the :class:`~boardman.agent.brain.ProjectState` the caller already built.
    Everything answered from it is a fact the process already had in memory, so the
    question costs no LLM call, no tool round trip and no network request. Anything the
    state cannot answer falls through to the normal reasoning path — a wrong fast answer
    is far more expensive than a slow one.
    """

    text = (message or "").strip()
    if not text:
        return None
    if _WRITE_WORD.search(text):
        # Nothing here writes. A message that asks for one must reach the agent, even if
        # it also happens to match a read-shaped pattern.
        return None

    from_state = _answer_from_state(text, state)
    if from_state is not None:
        return from_state

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
