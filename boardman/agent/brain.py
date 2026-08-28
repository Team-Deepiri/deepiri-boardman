"""What Boardman knows about a repo before anyone asks it anything.

Three layers, assembled with **no network calls at all**:

* **L0 identity** — which repo, which board, which group, which branch. Pure functions
  over ``repos.yml`` and the session. This was always instant; it just was not collected
  anywhere, so the model was told to go and find it.
* **L1 briefing** — the ``ProjectContext`` snapshot: purpose, structure, important paths,
  DIRECTION and README. Already persisted, already survives a restart, already carries the
  source revision it was built from.
* **L2 live state** — open issues, active PRs and what the sync did most recently, read
  from the four SQLite tables the GitHub→Plaky sync already fills on every webhook. This
  is the layer that did not exist: the rows were being written and nobody read them, so a
  question about live state went to the GitHub API for facts already sitting on disk.

The point is not to add knowledge. It is to stop paying for knowledge twice, and to give
the model a structured answer instead of instructions on how to look one up.

Everything here is read-only. Nothing in this module writes to Plaky, GitHub or the DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.database.models import IssueTaskMap, ProjectContext, PullRequestTaskLink, SyncLog

logger = __import__("logging").getLogger(__name__)

# How far back "recent activity" looks. Long enough to cover a working day, short enough
# that "recently" means something.
_ACTIVITY_WINDOW = timedelta(days=3)
_ACTIVITY_LIMIT = 12

# Sync actions worth telling a human about. The log carries roughly thirty kinds, most of
# them bookkeeping; these are the ones that answer "what happened".
_NOTABLE_ACTIONS = {
    # Verified against the action strings the handlers actually write AND against the live
    # sync_log. Six of the names here used to be invented, so issue closes, reopens, PR
    # closures and reviews were dropped and the assistant reported a busy repo as quiet.
    # tests/test_brain.py pins every key against the source so it cannot drift again.
    "issue_created": "task created from issue",
    "issue_closed": "issue closed",
    "issue_reopened": "issue reopened",
    "issue_edited_synced": "issue text updated",
    "issue_labels_synced": "issue labels synced",
    "pr_linked": "PR linked",
    "pr_linked_fuzzy": "PR linked (fuzzy match)",
    "pr_ambiguous_triage": "task created from PR",
    "pr_merged": "PR merged",
    "pr_closed_without_merge": "PR closed without merging",
    "pr_metadata_synced": "PR metadata synced",
    "pr_labels_synced": "PR labels synced",
    "pr_review_plaky_status": "review moved the task",
    "pr_review_synced": "review synced",
    "pr_review_dismissed": "review dismissed",
    "pr_resubmitted_needs_qa_again": "fix pushed, back to QA",
    "pr_converted_to_draft": "PR went back to draft",
}


@dataclass(frozen=True)
class Identity:
    """L0. Known without touching anything but config."""

    repo_full_name: str = ""
    repo_short: str = ""
    board_id: str = ""
    group_id: str = ""
    table: str = ""
    category: str = ""
    default_branch: str = ""
    source: str = ""

    @property
    def routed(self) -> bool:
        return bool(self.board_id and self.group_id)


@dataclass(frozen=True)
class Briefing:
    """L1. The persisted snapshot plus how old it is."""

    payload: dict[str, Any] = field(default_factory=dict)
    state: str = "miss"  # miss | fresh | stale | unavailable
    age_seconds: float | None = None
    source_revision: str = ""

    @property
    def present(self) -> bool:
        return bool(self.payload)


@dataclass(frozen=True)
class TrackedPR:
    number: int
    task_id: str
    issue_number: int
    link_source: str


@dataclass(frozen=True)
class Activity:
    action: str
    ref: str
    when: datetime | None
    task_id: str = ""


@dataclass(frozen=True)
class LiveState:
    """L2. Everything the sync engine already knows, read from SQLite."""

    tracked_issues: list[int] = field(default_factory=list)
    # One row per (repo, PR, issue), so a PR closing three issues appears three times.
    # `active_prs` keeps every row because the issue-to-task lookup needs them; the COUNTS
    # are distinct PR numbers, which is what anybody asking "how many PRs" means.
    active_prs: list[TrackedPR] = field(default_factory=list)
    open_pr_count: int = 0
    merged_prs: int = 0
    recent: list[Activity] = field(default_factory=list)
    last_event_at: datetime | None = None
    available: bool = True


@dataclass(frozen=True)
class ProjectState:
    identity: Identity
    briefing: Briefing
    live: LiveState

    @property
    def known(self) -> bool:
        """True when there is anything worth putting in front of the model."""
        return bool(self.identity.repo_full_name) and (
            self.briefing.present or self.live.tracked_issues or self.live.active_prs
        )


def resolve_identity(
    repo: str,
    *,
    default_branch: str = "",
    board_id: str = "",
    group_id: str = "",
) -> Identity:
    """L0 for a repo name, from config only. Never raises, never blocks.

    ``board_id`` / ``group_id`` are the placement the CALLER already resolved. That matters:
    repos.yml pins exactly one repo, while the sync engine also discovers placement from
    the Plaky catalog, so reading repos.yml alone reported most of the org as unrouted when
    every one of them has a real board and group.
    """
    name = (repo or "").strip()
    if not name:
        return Identity()
    short = name.rsplit("/", 1)[-1]
    try:
        from boardman.repos_config import get_routing
        from boardman.settings import settings

        routing = get_routing(name, short, settings.github_org)
    except (ImportError, AttributeError, KeyError, TypeError) as exc:
        logger.debug("routing unavailable for %s: %s", name, exc)
        routing = None
    if routing is None:
        return Identity(
            repo_full_name=name,
            repo_short=short,
            board_id=(board_id or "").strip(),
            group_id=(group_id or "").strip(),
            default_branch=default_branch,
        )
    return Identity(
        repo_full_name=name,
        repo_short=short,
        # The caller's resolved placement wins: it is what the write will actually use.
        board_id=(board_id or "").strip()
        or str(getattr(routing, "plaky_board_id", "") or "").strip(),
        group_id=(group_id or "").strip()
        or str(getattr(routing, "plaky_group_id", "") or "").strip(),
        table=str(getattr(routing, "plaky_table", "") or "").strip(),
        category=str(getattr(routing, "category", "") or "").strip(),
        default_branch=default_branch,
        source=str(getattr(routing, "description", "") or "").strip(),
    )


async def _load_briefing(session: AsyncSession | None, repo: str) -> Briefing:
    """L1 straight from the ProjectContext row, with its age."""
    if session is None or not repo:
        return Briefing()
    # Casefolded, because that is how every write stores it. Comparing the raw value
    # against a BINARY-collated column meant `Team-Deepiri/x` never found the row saved as
    # `team-deepiri/x`: the briefing was permanently a miss, so the default-branch fast
    # path never fired and `schedule_revalidation` re-enqueued a refresh job every five
    # minutes forever -- one that writes the casefolded row and so can never satisfy its
    # own lookup.
    key = (repo or "").strip().casefold()
    try:
        row = (
            await session.execute(select(ProjectContext).where(ProjectContext.repo == key))
        ).scalar_one_or_none()
    except SQLAlchemyError:
        # Migration 005 may not have run yet on a rolling deploy. Context is an
        # optimisation; chat must never be unavailable because of it.
        await session.rollback()
        return Briefing(state="unavailable")
    if row is None or not row.context_json:
        return Briefing()
    try:
        payload = json.loads(row.context_json)
    except (TypeError, ValueError):
        return Briefing()
    if not isinstance(payload, dict) or not payload.get("ok"):
        return Briefing()

    age = None
    if row.context_fetched_at is not None:
        age = (datetime.utcnow() - row.context_fetched_at).total_seconds()
    from boardman.settings import settings

    ttl = float(settings.agent_repo_context_cache_ttl_seconds or 0.0)
    state = "fresh" if age is not None and age <= ttl else "stale"
    return Briefing(
        payload=payload,
        state=state,
        age_seconds=round(age, 1) if age is not None else None,
        source_revision=str(row.context_source_revision or ""),
    )


async def _load_live(session: AsyncSession | None, identity: Identity) -> LiveState:
    """L2 from the sync engine's own tables. Four indexed selects, no network."""
    short = identity.repo_short
    if session is None or not short:
        return LiveState(available=False)
    # The sync engine writes whatever casing GitHub sent; a user typing "Deepiri-Boardman"
    # resolves to the same repo but would match no rows, and the assistant would report a
    # busy repo as having nothing on the board at all.
    short_ci = func.lower(short)
    try:
        issues = list(
            (
                await session.execute(
                    select(IssueTaskMap.github_issue_number)
                    .where(func.lower(IssueTaskMap.github_repo) == short_ci)
                    .order_by(IssueTaskMap.github_issue_number.desc())
                )
            ).scalars()
        )
        # "Active" is the sync engine's own definition of a live PR link: not merged and
        # not withdrawn. Reusing it means the Brain and the board can never disagree.
        pr_rows = list(
            (
                await session.execute(
                    select(PullRequestTaskLink)
                    .where(
                        func.lower(PullRequestTaskLink.github_repo) == short_ci,
                        PullRequestTaskLink.merged_at.is_(None),
                        PullRequestTaskLink.withdrawn_at.is_(None),
                    )
                    .order_by(PullRequestTaskLink.github_pr_number.desc())
                )
            ).scalars()
        )
        merged = len(
            {
                int(n)
                for n in (
                    await session.execute(
                        select(PullRequestTaskLink.github_pr_number).where(
                            func.lower(PullRequestTaskLink.github_repo) == short_ci,
                            PullRequestTaskLink.merged_at.is_not(None),
                        )
                    )
                ).scalars()
            }
        )
        since = datetime.utcnow() - _ACTIVITY_WINDOW
        log_rows = list(
            (
                await session.execute(
                    select(SyncLog)
                    .where(func.lower(SyncLog.github_repo) == short_ci, SyncLog.created_at >= since)
                    .order_by(SyncLog.created_at.desc())
                    .limit(60)
                )
            ).scalars()
        )
    except SQLAlchemyError:
        await session.rollback()
        return LiveState(available=False)

    seen: set[tuple[str, str]] = set()
    recent: list[Activity] = []
    for row in log_rows:
        label = _NOTABLE_ACTIONS.get(str(row.action or ""))
        if not label:
            continue
        key = (label, str(row.github_ref or ""))
        if key in seen:  # the same issue synced eight times is one line, not eight
            continue
        seen.add(key)
        recent.append(
            Activity(
                action=label,
                ref=str(row.github_ref or ""),
                when=row.created_at,
                task_id=str(row.plaky_task_id or ""),
            )
        )
        if len(recent) >= _ACTIVITY_LIMIT:
            break

    active = [
        TrackedPR(
            number=int(r.github_pr_number),
            task_id=str(r.plaky_task_id or ""),
            issue_number=int(r.github_issue_number or 0),
            link_source=str(r.link_source or ""),
        )
        for r in pr_rows
        if not str(r.plaky_task_id or "").startswith("pending:")
    ]
    return LiveState(
        tracked_issues=[int(n) for n in issues],
        active_prs=active,
        open_pr_count=len({p.number for p in active}),
        merged_prs=merged,
        recent=recent,
        last_event_at=log_rows[0].created_at if log_rows else None,
    )


async def get_project_state(
    session: AsyncSession | None,
    repo: str,
    *,
    board_id: str = "",
    group_id: str = "",
) -> ProjectState:
    """Everything known about ``repo`` without asking anyone.

    Safe to call on the interactive path: L0 is pure, L1 is one indexed row, L2 is four
    indexed selects against a local SQLite file. It never calls GitHub or Plaky, so it can
    never be the reason a chat turn is slow.
    """
    briefing = await _load_briefing(session, repo)
    structure = briefing.payload.get("structure") if briefing.present else None
    branch = ""
    if isinstance(structure, dict):
        candidate = str(structure.get("default_branch") or "").strip()
        branch = "" if candidate.casefold() in ("", "unknown") else candidate
    identity = resolve_identity(repo, default_branch=branch, board_id=board_id, group_id=group_id)
    live = await _load_live(session, identity)
    return ProjectState(identity=identity, briefing=briefing, live=live)


#: repo -> monotonic time we last asked for a refresh. Stops a stale repo from enqueuing a
#: refresh on every single turn while the first one is still running.
_revalidating: dict[str, float] = {}
_revalidation_tasks: set[Any] = set()
_REVALIDATE_COOLDOWN = 300.0


def schedule_revalidation(state: ProjectState) -> bool:
    """Refresh a stale or missing briefing in the background. True if one was started.

    The stale-while-revalidate half that was missing: the snapshot was already served to
    the caller, and this makes the next caller's copy current.

    Returns without awaiting anything. Queuing the job writes a ``background_jobs`` row,
    and a SQLite write on the request path can block behind another turn's write lock —
    the chat path must never wait on work whose entire purpose is to be off it.
    """
    import asyncio
    import time

    repo = state.identity.repo_full_name
    if "/" not in repo or state.briefing.state in ("fresh", "unavailable"):
        return False
    now = time.monotonic()
    last = _revalidating.get(repo)
    if last is not None and (now - last) < _REVALIDATE_COOLDOWN:
        return False
    _revalidating[repo] = now

    async def _queue() -> None:
        try:
            from boardman.jobs.deferred import enqueue_and_run_soon
            from boardman.observability.counters import background_work, bump

            with background_work():
                await enqueue_and_run_soon("boardman_repo_refresh_job", {"repo": repo})
                bump("brain.revalidations")
        except (
            OSError,
            RuntimeError,
            Exception,
        ) as exc:  # noqa: BLE001 — queue down is invisible to the user
            _revalidating.pop(repo, None)
            logger.debug("revalidation queue failed for %s: %s", repo, exc)

    task = asyncio.create_task(_queue(), name=f"revalidate:{repo}")
    # Hold a reference: an un-awaited task can be garbage collected mid-flight.
    _revalidation_tasks.add(task)
    task.add_done_callback(_revalidation_tasks.discard)
    return True


def _age_phrase(seconds: float | None) -> str:
    if seconds is None:
        return "age unknown"
    if seconds < 90:
        return "seconds ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def render_project_state(state: ProjectState, *, max_chars: int = 6500) -> str:
    """The project state as compact structured context, not prose.

    Facts first and cheapest: identity and routing, then live state from the sync tables,
    then the briefing text. The text is capped hard because it is the expensive part and
    the least specific — the structured lines above it answer more per character.
    """
    if not state.identity.repo_full_name:
        return ""

    ident, live, brief = state.identity, state.live, state.briefing
    structure = brief.payload.get("structure") if brief.present else {}
    structure = structure if isinstance(structure, dict) else {}

    lines: list[str] = ["\n## Project state (cached; no lookup needed for these facts)"]
    lines.append(f"- repo: `{ident.repo_full_name}`")
    if ident.board_id:
        table = f" ({ident.table})" if ident.table else ""
        group = (
            f" group `{ident.group_id}`"
            if ident.group_id
            else " group resolved from the board when work is filed"
        )
        lines.append(f"- plaky: board `{ident.board_id}`{group}{table}")
    else:
        # NOT "this repo has no routing". resolve_identity reads repos.yml only, while the
        # sync engine also auto-discovers placement from the Plaky catalog — so a repo
        # absent here is still routed and does get tasks filed. Saying otherwise told the
        # model a repo was untracked when the board had been taking its work for weeks.
        lines.append(
            "- plaky: not pinned in repos.yml; the sync engine discovers its board and "
            "group when it files work. Do not say this repo is untracked."
        )
    if ident.default_branch:
        lines.append(f"- default branch: `{ident.default_branch}`")
    if structure.get("language"):
        lines.append(f"- language: {structure.get('language')}")
    if structure.get("description"):
        lines.append(f"- description: {structure.get('description')}")
    dirs = [str(x) for x in (structure.get("top_level_dirs") or [])][:12]
    if dirs:
        lines.append(f"- top level: {', '.join(dirs)}")
    paths = [str(x) for x in (structure.get("important_paths") or [])][:18]
    if paths:
        lines.append(f"- important paths: {', '.join(paths)}")

    if live.available:
        lines.append("")
        lines.append(f"- issues tracked on the board: {len(live.tracked_issues)}")
        if live.tracked_issues:
            head = ", ".join(f"#{n}" for n in live.tracked_issues[:14])
            more = "" if len(live.tracked_issues) <= 14 else f" (+{len(live.tracked_issues) - 14})"
            lines.append(f"- issue numbers: {head}{more}")
        lines.append(
            # `open_pr_count` is the number of PRs; `active_prs` is the number of LINK
            # ROWS, and one PR that closes three issues has three of those.
            f"- PRs open against a task: {live.open_pr_count}, merged so far: "
            f"{live.merged_prs}"
        )
        for pr in live.active_prs[:8]:
            issue = f" for issue #{pr.issue_number}" if pr.issue_number else " (no linked issue)"
            lines.append(f"  - PR #{pr.number}{issue} -> task {pr.task_id}")
        if live.recent:
            lines.append("- recent sync activity:")
            for row in live.recent[:8]:
                ref = f" #{row.ref}" if row.ref else ""
                lines.append(f"  - {row.action}{ref}")

    if brief.present:
        rev = f", from `{brief.source_revision[:12]}`" if brief.source_revision else ""
        lines.append(f"\n_briefing captured {_age_phrase(brief.age_seconds)}{rev}._")
        # Capped hard: this is the expensive part of the block and the least specific.
        # The structured lines above answer more per character than another 2kB of README.
        for label, key, cap in (
            ("Direction", "DIRECTION_md", 1400),
            ("README", "readme_md", 1200),
            ("Code signals", "code_signals", 700),
        ):
            value = brief.payload.get(key)
            if isinstance(value, str) and value.strip() and not value.strip().startswith("("):
                lines.append(f"\n### {label}\n{value[:cap].strip()}")
    else:
        lines.append(
            "\n_No cached briefing for this repo yet. Use the repo tools if the question "
            "needs code or docs; the facts above are still current._"
        )

    return "\n".join(lines)[:max_chars]
