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


# "what IS the default branch" only. The earlier pattern fired on any sentence containing
# "what branch" or "main branch" — including "what branch should I cut this fix from" and
# "why did main branch CI go red" — and ended the turn with a one-line branch name.
_DEFAULT_BRANCH = re.compile(
    r"^\s*(?:what(?:'s| is)?|which)\s+(?:is\s+)?(?:the\s+)?"
    r"(?:default|main|base|primary)\s+branch\b"
    r"|\bwhat(?:'s| is)\s+(?:the\s+)?default\s+branch\b"
    r"|\bdefault\s+branch\s+(?:is|of|for)\b",
    re.IGNORECASE,
)
# "which task is issue 91", "is there a task for issue 4242". The number must be attached
# to the word issue or a #, otherwise "which task is 3 days old" read 3 as an issue number
# and asserted it was unmapped.
_ISSUE_NUMBER = re.compile(
    r"\bissue\s*#?(\d{1,6})\b.{0,40}\b(?:task|plaky|card|tracked|linked)\b"
    r"|\b(?:task|plaky|card)\b.{0,40}\b(?:for|of|from|is|linked\s+to)\s+"
    r"(?:issue\s*#?|#)(\d{1,6})\b",
    re.IGNORECASE,
)
# The mapping tables answer "how many are ON THE BOARD", never "how many are open on
# GitHub" — a closed issue keeps its row forever. A question that says "open" is therefore
# a different question and must not be answered from here.
_HOW_MANY_ISSUES = re.compile(
    r"\bhow\s+many\b[^?]{0,30}\bissues?\b|\bissue\s+count\b|\bcount\s+of\s+issues\b",
    re.IGNORECASE,
)
_HOW_MANY_PRS = re.compile(
    r"\bhow\s+many\b[^?]{0,30}\b(?:prs?|pull\s+requests?)\b|\bpr\s+count\b",
    re.IGNORECASE,
)
# Anything asking for the CURRENT state of the world means go and look. Deliberately
# generous: a needless LLM call costs seconds, a stale answer costs trust.
_ASKS_LIVE = re.compile(
    r"\b(?:right\s+now|now|currently|current|today|live|latest|at\s+the\s+moment|"
    r"check\s+github|as\s+of\s+now|up\s+to\s+date|still)\b",
    re.IGNORECASE,
)
# "open"/"closed"/"merged" are GitHub facts. The board mapping knows what was ever synced,
# not what is open, so questions about open work are not ours to answer from cache.
_ASKS_OPEN = re.compile(r"\b(?:open|closed|merged|unresolved|outstanding)\b", re.IGNORECASE)
# An owner/name slug mentioned anywhere in the question.
_REPO_MENTION = re.compile(r"\b([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)\b")


class TrackedPRView:
    """One pull request, gathering the issues its link rows name."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.issues: set[int] = set()

    def issue_suffix(self) -> str:
        if not self.issues:
            return ""
        nums = ", ".join(f"#{i}" for i in sorted(self.issues))
        return f" (issue{'s' if len(self.issues) > 1 else ''} {nums})"


def _names_a_different_repo(text: str, ident: Any) -> bool:
    """True when the question is plainly about some OTHER repo than the state describes.

    The session repo only switches for slugs listed in repos.yml, which holds one entry,
    so "what is the default branch of deepiri-sorge?" arrives carrying boardman's state.
    Answering from it is a confident, instant, wrong answer about the wrong project.
    """
    short = str(getattr(ident, "repo_short", "") or "").casefold()
    full = str(getattr(ident, "repo_full_name", "") or "").casefold()
    for slug in _REPO_MENTION.findall(text):
        low = slug.casefold()
        if low != full and low.rsplit("/", 1)[-1] != short:
            return True
    # Bare short names, restricted to shapes that look like this org's repos so ordinary
    # hyphenated words never trip it.
    for word in re.findall(r"\b(?:deepiri|diri)-[a-z0-9-]+\b", text.casefold()):
        if word != short:
            return True
    return False


def _answer_from_state(text: str, state: Any | None) -> FastPathResult | None:
    """Answer from project state, or None to let the reasoning path handle it.

    Only questions whose whole answer is a stored fact. Anything needing judgment,
    ranking, code, or a value that could have changed since the last webhook goes to the
    agent — the cost of being wrong here is a confident, instant falsehood.
    """
    if state is None or not getattr(getattr(state, "identity", None), "repo_full_name", ""):
        return None
    ident, live = state.identity, state.live
    if _names_a_different_repo(text, ident):
        # The session repo only changes for slugs listed in repos.yml. Without this, a
        # question about deepiri-sorge is answered from boardman's state.
        return None
    # "right now", "currently", "today" all mean go and look. Once, at the top, so no
    # intent can be added later that quietly forgets to check.
    if _ASKS_LIVE.search(text):
        return None

    if _DEFAULT_BRANCH.search(text) and ident.default_branch:
        return FastPathResult(
            f"`{ident.repo_full_name}` builds from `{ident.default_branch}`.",
            "default_branch",
        )

    # Counts come from the sync engine's own mapping tables, so they describe the BOARD:
    # every issue ever synced, closed ones included. A question about OPEN issues is a
    # GitHub question and belongs to the agent.
    asks_open = bool(_ASKS_OPEN.search(text))
    if live.available:
        if _HOW_MANY_ISSUES.search(text) and not asks_open:
            n = len(live.tracked_issues)
            if n:
                head = ", ".join(f"#{x}" for x in live.tracked_issues[:10])
                more = "" if n <= 10 else f" and {n - 10} more"
                return FastPathResult(
                    f"{n} issue{'s' if n != 1 else ''} from `{ident.repo_full_name}` have a "
                    f"Plaky task, open and closed together: {head}{more}. Ask about open "
                    f"issues if you want the live GitHub count.",
                    "issue_count",
                )
            return FastPathResult(
                f"No issues from `{ident.repo_full_name}` are mapped to a Plaky task yet.",
                "issue_count",
            )

        if _HOW_MANY_PRS.search(text) and not asks_open:
            n = live.open_pr_count
            if n:
                # One line per PULL REQUEST. pr_task_links holds a row per (PR, issue), so
                # listing rows printed a PR that closes three issues three times, and
                # counting rows called one PR "three pull requests".
                by_pr: dict[int, TrackedPRView] = {}
                for link in live.active_prs:
                    view = by_pr.setdefault(link.number, TrackedPRView(link.task_id))
                    if link.issue_number:
                        view.issues.add(link.issue_number)
                rows = "\n".join(
                    f"- PR #{num} -> task `{view.task_id}`" + view.issue_suffix()
                    for num, view in list(by_pr.items())[:10]
                )
                return FastPathResult(
                    f"{n} pull request{'s' if n != 1 else ''} on `{ident.repo_full_name}` "
                    f"{'are' if n != 1 else 'is'} linked to a live task, and "
                    f"{live.merged_prs} {'have' if live.merged_prs != 1 else 'has'} "
                    f"merged:\n{rows}",
                    "pr_count",
                )
            return FastPathResult(
                f"No pull requests on `{ident.repo_full_name}` are linked to a live task; "
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
                where = f" on board `{ident.board_id}`" if ident.board_id else ""
                return FastPathResult(
                    f"Issue #{number} has a Plaky task{where}.",
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
