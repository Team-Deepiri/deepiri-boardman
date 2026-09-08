"""Poll GitHub repo events and replay them through the webhook handlers.

Local "as-if-production" substitute for the GitHub webhook (TESTING_LIVE_PLAKY=true):
GitHub cannot deliver webhooks to a developer machine, so while this instance runs we
poll GET /repos/{owner}/{repo}/events for each repo in TESTING_LIVE_PLAKY_REPOS and
dispatch every NEW event through the same parse + handler path used by
POST /api/v1/webhooks/github. Plaky therefore updates live only while the process runs.

Semantics:
- On the first poll of each repo we record the newest event id as a baseline and process
  nothing older — history from before startup is never replayed into Plaky.
- The public events feed exposes a subset of webhook actions (issues opened, PRs
  opened/closed/reopened, reviews, comments, pushes). review_requested and
  ready_for_review only arrive via real webhooks.
- PushEvent has no webhook-handler equivalent: commits whose message references an
  issue ("#12", "Fixes #12") are commented onto the linked Plaky task.

In production set TESTING_LIVE_PLAKY=false — the poller never starts and the
registered GitHub webhook delivers events instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from boardman.database.models import SyncLog
from boardman.database.session import async_session
from boardman.github.auth import github_auth_available, github_auth_header
from boardman.github.change_signal import note_repo_changed, repo_full_name_from_payload
from boardman.github.webhooks import (
    IssueCommentEventPayload,
    IssueEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
    parse_webhook_payload,
)
from boardman.observability.degradation import log_degraded
from boardman.services.comment_dedupe import comment_already_synced
from boardman.services.issue_handler import find_plaky_task_by_issue, handle_issue_opened
from boardman.services.pr_handler import (
    handle_pr_closed_without_merge,
    handle_pr_merged,
    handle_pr_opened,
    handle_pr_review_comment,
)
from boardman.services.pr_review_handler import (
    handle_issue_comment_on_pr,
    handle_pull_request_review,
)
from boardman.settings import settings

_log = logging.getLogger(__name__)

# Link policy (deliberate, per Sorge review discussion): commit messages accept a bare
# "#12" because commits are informal and a mention is cheap (one comment on the task).
# PR/issue LINKING (issue_handler.ISSUE_LINK_RE) requires closing keywords because a
# link drives status, assignee and QA; bare mentions there go through the fuzzy
# pipeline, which corroborates before linking.
# Commit messages referencing issues: "Fixes #12", "closes #3", or a bare "#12".
_COMMIT_ISSUE_RE = re.compile(
    r"(?:(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+)?#(\d+)", re.IGNORECASE
)


def issue_meta_signature(it: dict[str, Any]) -> tuple[str, ...]:
    """Everything metadata-shaped that must re-sync the task when it changes on GitHub:
    native Type, the sidebar Priority field, assignees, labels."""
    type_name = ""
    if isinstance(it.get("type"), dict):
        type_name = str(it["type"].get("name") or "")
    priority = ""
    for row in it.get("issue_field_values") or []:
        if (
            isinstance(row, dict)
            and str(row.get("issue_field_name") or "").strip().casefold() == "priority"
        ):
            opt = row.get("single_select_option")
            priority = str(opt.get("name") or "") if isinstance(opt, dict) else ""
            break
    assignee_sig = tuple(
        sorted(
            str((a or {}).get("login") or "")
            for a in (it.get("assignees") or [])
            if isinstance(a, dict)
        )
    )
    labels = tuple(
        sorted(
            str((lb or {}).get("name") or "")
            for lb in (it.get("labels") or [])
            if isinstance(lb, dict)
        )
    )
    return (type_name, priority) + assignee_sig + labels


def issue_text_signature(it: dict[str, Any]) -> tuple[str, str]:
    """Title plus a body digest — a change means the issue was edited and the task's
    name/description must follow (bodies can be huge; the digest keeps the map small)."""
    body = str(it.get("body") or "")
    return (
        str(it.get("title") or ""),
        hashlib.sha1(body.encode("utf-8"), usedforsecurity=False).hexdigest(),
    )


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# Events-feed type -> webhook event name understood by parse_webhook_payload.
_EVENT_TYPE_TO_WEBHOOK = {
    "IssuesEvent": "issues",
    "PullRequestEvent": "pull_request",
    "PullRequestReviewEvent": "pull_request_review",
    "PullRequestReviewCommentEvent": "pull_request_review_comment",
    "IssueCommentEvent": "issue_comment",
}

# Event types the events-feed poll is responsible for. Issues/PRs/pushes are handled by the
# real-time direct poll instead (the events feed lags several minutes); keeping them here too
# would double-process. Reviews and comments have no cheap real-time "since" list, so they stay.
EVENTS_FEED_TYPES = frozenset(
    {"PullRequestReviewEvent", "PullRequestReviewCommentEvent", "IssueCommentEvent"}
)


# Sentinels that mean "watch every eligible repo" instead of a hand-written list.
_WATCH_ALL_TOKENS = {"*", "all", "auto"}
# `get_routing`'s source for the repos.yml `defaults` block, which answers for every repo
# in the org. Fine when a human named the repo; not a destination when we are sweeping.
_ORG_DEFAULT_SOURCE = "org_default"
# How many poll cycles between re-resolving the watch list. Repos gain Plaky groups and
# new repos appear; a list resolved once at startup also strands the poller on nothing if
# that first org listing happened to fail.
_WATCH_LIST_REFRESH_CYCLES = 20
# GitHub allows 5000 authenticated requests/hour, and the agent's own tools spend from the
# same budget. Per repo per cycle the poller makes exactly four calls that do not depend on
# what the repo contains: issues, pulls, the events feed, and the default-branch commits.
_FIXED_CALLS_PER_REPO_PER_CYCLE = 4
# Plus one /commits call per OPEN PR head branch, which is not a constant -- a busy repo
# with a dozen live PRs costs four times what a quiet one does. A fixed per-repo guess was
# wrong in the direction that matters: 31 repos each holding five open PRs is ~4,500
# calls/hour at an interval the throttle called safe, past both the poller's own budget and
# GitHub's whole allowance. So the estimate counts the branches actually being tracked, and
# the interval is recomputed each cycle as that number moves.
_ASSUMED_OPEN_PRS_PER_REPO = 2
# How many withdrawn PRs a cycle may ask about directly. Normally zero: a link is only
# withdrawn between a close and the next sighting of the PR, and handling one clears it.
# The cap is there so a repo that somehow accumulates them cannot spend the whole budget.
_MAX_REOPEN_PROBES_PER_CYCLE = 5
# (repo count, chosen interval) pairs already explained in the log. See `_safe_interval`.
_INTERVAL_WARNED: set[tuple[int, int]] = set()


def cycle_call_estimate(repo_count: int, branch_count: int | None = None) -> float:
    """GitHub calls one poll cycle costs, from the open PR branches actually tracked.

    `branch_count` is None before the first cycle has run, when nothing is tracked yet and
    zero would understate the cost of every repo. An allowance stands in until observation
    replaces it, and from then on the real number is used -- including a real zero, which
    is what a set of quiet repos genuinely costs.
    """
    if repo_count <= 0:
        return 0.0
    branches = (
        repo_count * _ASSUMED_OPEN_PRS_PER_REPO if branch_count is None else max(0, branch_count)
    )
    return repo_count * _FIXED_CALLS_PER_REPO_PER_CYCLE + branches


# What the poller may spend of GitHub's 5,000/hour, leaving the rest for the assistant --
# the interactive thing, and a poller that starves it has made the product worse to keep a
# background loop punctual.
#
# This DOES slow the configuration that was already running: 3 repos at 15s is ~4,300
# calls/hour once open PR branches are counted, not the 2,880 an earlier version of this
# comment claimed from a four-call estimate. That config was quietly spending most of the
# hourly allowance. 15s becomes about 22s, which is the honest price of the interval.
_POLLER_HOURLY_CALL_BUDGET = 3000


def track_pr_branch(branches: dict[int, str], pr_number: int, head_ref: str, state: str) -> None:
    """Remember the head ref of an OPEN PR, and forget it once the PR is not.

    Keyed by PR NUMBER, not by branch. Two PRs can share a head ref -- close one and open
    another from the same branch -- and keying on the ref let the closed one drop a branch
    the live one still needs, silently ending commit polling for it. Forgetting on close is
    what keeps this from growing forever: each entry costs one /commits call per cycle,
    which is what the rate budget is built on.
    """
    if head_ref and state == "open":
        branches[pr_number] = head_ref
    else:
        branches.pop(pr_number, None)


def watch_all_requested() -> bool:
    """True when TESTING_LIVE_PLAKY_REPOS asks for every eligible repo rather than a list."""
    raw = (settings.testing_live_plaky_repos or "").strip().casefold()
    return raw in _WATCH_ALL_TOKENS


def poller_repos() -> list[str]:
    """The explicitly configured repo list. Empty when the config asks to watch everything.

    Kept sync and literal: it is what the config SAYS. `resolve_poller_repos` is what the
    poller actually watches, because "everything" needs the org listing and each repo's
    Plaky routing before it is knowable.
    """
    if watch_all_requested():
        return []
    out: list[str] = []
    for chunk in (settings.testing_live_plaky_repos or "").replace("\n", ",").split(","):
        s = chunk.strip()
        if s and "/" in s and s not in out:
            out.append(s)
    return out


async def resolve_poller_repos() -> tuple[list[str], list[tuple[str, str]]]:
    """(repos to watch, [(repo, why it was excluded)]).

    An explicit TESTING_LIVE_PLAKY_REPOS is honoured as-is: naming a repo is a decision,
    and second-guessing it would make the setting useless for debugging one repo.

    Otherwise every repo in the org is a candidate, and a repo is watched only when it can
    actually be synchronized: not archived, and resolving to a real Plaky board. The
    excluded ones are RETURNED, not silently dropped -- "diri-cyrex is archived" and
    "diva has no Plaky board" are the answer to "why isn't my repo syncing", and that
    answer has to be visible without reading the code.
    """
    explicit = poller_repos()
    if explicit:
        return explicit, []
    if not watch_all_requested():
        return [], []

    org = (settings.github_org or "").strip()
    if not org or not github_auth_available():
        return [], [("(org listing)", "GITHUB_ORG and GITHUB_PAT are both required")]

    from boardman.github.http import github_http_client
    from boardman.github.org_repos import fetch_org_repository_full_names
    from boardman.repos_config import get_routing_async

    try:
        # skip_archived: an archived repo cannot receive new activity, so watching it
        # spends rate limit on a guaranteed-empty answer.
        names = await fetch_org_repository_full_names(github_http_client(), org, skip_archived=True)
    except Exception as exc:  # noqa: BLE001 - the poller must not die on a listing failure
        log_degraded(_log, f"resolve_poller_repos: listing {org}", exc)
        return [], [("(org listing)", f"could not list {org}: {type(exc).__name__}: {exc}")]

    watched: list[str] = []
    excluded: list[tuple[str, str]] = []
    for full in sorted({str(n).strip() for n in names if str(n or "").strip()}):
        short = full.rsplit("/", 1)[-1]
        try:
            # with_source: a board is only a destination if we can say WHERE it came
            # from -- an explicit repos.yml entry, or a Plaky group actually named after
            # the repo. Logging the source is what makes a wrong placement findable.
            routing, source = await get_routing_async(full, short, org, with_source=True)
        except Exception as exc:  # noqa: BLE001 - one bad repo must not stop the fleet
            log_degraded(_log, f"resolve_poller_repos: routing for {full}", exc)
            excluded.append((full, f"routing lookup failed: {type(exc).__name__}"))
            continue
        board_id = str(getattr(routing, "plaky_board_id", "") or "").strip()
        if not board_id:
            # Never invent a destination: a task written to the wrong board is worse
            # than a repo that visibly is not being watched.
            excluded.append((full, "no Plaky board resolves for this repo"))
            continue
        if str(source or "").startswith(_ORG_DEFAULT_SOURCE):
            # repos.yml `defaults` answers for EVERY repo in the org, so accepting it
            # here would file all 48 repos' issues and PRs onto one shared board and call
            # that a destination. A default is a fallback for a repo somebody chose to
            # configure, not evidence that this repo belongs anywhere.
            excluded.append((full, "only the org-default board resolves; no placement of its own"))
            continue
        _log.info(
            "TESTING_LIVE_PLAKY: watching %s -> board %s (placement: %s)", full, board_id, source
        )
        watched.append(full)
    return watched, excluded


class GitHubEventPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # repo full_name -> set of event ids already processed. GitHub event ids are NOT
        # comparable across event types (PushEvent ids live in a different, higher number
        # range than IssuesEvent/PullRequestEvent), so novelty must be tracked as a set and
        # ordering must use created_at — never numeric id comparison.
        self._seen_ids: dict[str, set[str]] = {}
        self._etags: dict[str, str] = {}
        # Real-time direct-poll state (issues/PRs/commits): baseline instant + processed keys.
        self._baseline_dt: dict[str, datetime] = {}
        self._processed: dict[str, dict[str, set]] = {}

    @staticmethod
    def _safe_interval(
        configured: float, repo_count: int, branch_count: int | None = None
    ) -> float:
        """Stretch the poll interval so the watch list fits inside the API budget.

        A 15s interval over three repos is 2,880 calls/hour. The same interval over the
        31 repos `all` resolves to is nearly 30,000 — six times GitHub's entire hourly
        allowance, shared with the agent's own tools. Rather than silently rate-limiting
        the assistant, slow the loop down to what the budget affords and say so.

        The cost per cycle is not fixed, so this is re-asked as open PRs come and go.
        """
        if repo_count <= 0:
            return configured
        per_cycle = cycle_call_estimate(repo_count, branch_count)
        cycles_per_hour = _POLLER_HOURLY_CALL_BUDGET / per_cycle if per_cycle > 0 else 0.0
        needed = 3600.0 / cycles_per_hour if cycles_per_hour > 0 else configured
        if needed <= configured:
            return configured
        # Once per distinct answer. This runs every cycle, so an unconditional warning
        # repeated the same four lines every 22 seconds for the life of the process, which
        # is how a real warning stops being read.
        told = (repo_count, round(needed))
        if told in _INTERVAL_WARNED:
            return needed
        if len(_INTERVAL_WARNED) > 64:
            _INTERVAL_WARNED.clear()
        _INTERVAL_WARNED.add(told)
        observed = (
            f"assuming {repo_count * _ASSUMED_OPEN_PRS_PER_REPO}"
            if branch_count is None
            else str(max(0, branch_count))
        )
        _log.warning(
            "TESTING_LIVE_PLAKY: %d repos (%s open PR branches) at %.0fs would spend ~%.0f "
            "GitHub calls/hour; polling every %.0fs instead to stay inside the ~%d/hour the "
            "poller may use",
            repo_count,
            observed,
            configured,
            (3600.0 / configured) * per_cycle,
            needed,
            _POLLER_HOURLY_CALL_BUDGET,
        )
        return needed

    def _tracked_branch_count(self, repos: list[str]) -> int:
        """Open PR head branches currently polled for commits, across the watched repos."""
        return sum(len(self._processed.get(r, {}).get("pr_branches") or {}) for r in repos)

    async def _gh_headers(self) -> dict[str, str]:
        return await github_auth_header()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="github-event-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        configured_interval = max(15.0, float(settings.testing_live_plaky_poll_seconds or 60.0))
        repos, excluded = await resolve_poller_repos()
        for repo, why in excluded:
            _log.warning("TESTING_LIVE_PLAKY: not watching %s — %s", repo, why)
        interval = self._safe_interval(configured_interval, len(repos))
        _log.info(
            "TESTING_LIVE_PLAKY: GitHub poller started — watching %d repo(s)=%s "
            "(%d excluded) interval=%.0fs (Plaky updates apply only while this instance runs)",
            len(repos),
            repos,
            len(excluded),
            interval,
        )
        if not repos:
            _log.warning("TESTING_LIVE_PLAKY: no eligible repos to watch; poller idle")
        cycles = 0
        while not self._stop.is_set():
            # Re-resolve periodically. The watch list is derived from the org listing and
            # each repo's Plaky placement, and both change: a repo gets a Plaky group, a
            # new repo appears, or the first resolve hit a transient GitHub failure and
            # left this loop with nothing to do for the life of the process.
            if cycles and cycles % _WATCH_LIST_REFRESH_CYCLES == 0:
                fresh, fresh_excluded = await resolve_poller_repos()
                # Covers recovery too: after a failed first resolve `repos` is empty, so
                # any non-empty result differs and is adopted.
                if fresh and set(fresh) != set(repos):
                    _log.info(
                        "TESTING_LIVE_PLAKY: watch list changed — now %d repo(s) (%d excluded)",
                        len(fresh),
                        len(fresh_excluded),
                    )
                    repos = fresh
            # Re-asked every cycle, because the cost of a cycle moves with the open PRs
            # the org happens to have right now, not with anything resolved at startup.
            was = interval
            interval = self._safe_interval(
                configured_interval,
                len(repos),
                self._tracked_branch_count(repos) if cycles else None,
            )
            if cycles and abs(interval - was) > 1.0:
                _log.info(
                    "TESTING_LIVE_PLAKY: poll interval now %.0fs (%d repos, %d open PR branches)",
                    interval,
                    len(repos),
                    self._tracked_branch_count(repos),
                )
            cycles += 1
            for repo in repos:
                # Real-time REST endpoints (no events-feed lag) for the creation/push actions,
                # then the events feed for reviews + comments (which have no simple "since" list).
                try:
                    await self._poll_direct(repo)
                except httpx.HTTPError as e:
                    _log.warning(
                        "poller: direct poll of %s failed (transient network): %s", repo, e
                    )
                except (
                    Exception
                ):  # noqa: BLE001 - observability failure must not affect the request
                    _log.exception("poller: unexpected error in direct poll of %s", repo)
                try:
                    await self._poll_repo(repo)
                except httpx.HTTPError as e:
                    _log.warning(
                        "poller: events poll of %s failed (transient network): %s", repo, e
                    )
                except (
                    Exception
                ):  # noqa: BLE001 - observability failure must not affect the request
                    _log.exception("poller: unexpected error polling events of %s", repo)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
        _log.info("TESTING_LIVE_PLAKY: GitHub poller stopped")

    # ── Real-time direct polling (issues / PRs / commits) ────────────────────────
    async def _poll_direct(self, full_name: str) -> None:
        if not github_auth_available():
            return
        baseline = self._baseline_dt.get(full_name)
        if baseline is None:
            catchup = max(0.0, float(settings.testing_live_plaky_catchup_minutes or 0.0))
            baseline = datetime.now(UTC) - timedelta(minutes=catchup)
            self._baseline_dt[full_name] = baseline
            self._processed[full_name] = {
                "issues_opened": set(),
                # number -> last seen state ("open"/"closed"), so close/reopen are detected.
                "issue_state": {},
                "prs_opened": set(),
                "prs_closed": set(),
                # number -> last seen draft flag, and number -> set of requested reviewer
                # logins. GitHub only sends ready_for_review / review_requested as webhook
                # events, but both states are visible on the /pulls list, so we detect the
                # TRANSITION here instead of needing a webhook endpoint at all.
                "pr_draft": {},
                "pr_reviewers": {},
                # Head branches of open PRs. GET /commits defaults to the DEFAULT branch, so
                # without these a developer's "Fixes #12" commit on a feature branch is never
                # seen — which is where essentially all real work happens.
                "pr_branches": {},
                "reopen_probed": set(),
                "commits": set(),
            }
            _log.info(
                "poller: %s real-time baseline = %s (only activity at/after this is applied; catchup %.0f min)",
                full_name,
                baseline.strftime("%Y-%m-%dT%H:%M:%SZ"),
                catchup,
            )
        proc = self._processed[full_name]
        since = baseline.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Guard each endpoint so one transient failure does not skip the others this cycle.
        async with httpx.AsyncClient(timeout=30) as client:
            for label, coro in (
                ("issues", self._poll_issues(client, full_name, baseline, since, proc)),
                ("pulls", self._poll_pulls(client, full_name, baseline, proc)),
                ("commits", self._poll_commits(client, full_name, since, proc)),
            ):
                try:
                    await coro
                except httpx.HTTPError as e:
                    _log.warning(
                        "poller: %s poll of %s failed (transient network): %s", label, full_name, e
                    )
                except (
                    Exception
                ):  # noqa: BLE001 - observability failure must not affect the request
                    _log.exception("poller: %s poll of %s errored", label, full_name)

    async def _poll_issues(self, client, full_name, baseline, since, proc) -> None:
        owner, _, name = full_name.partition("/")
        url = (
            f"https://api.github.com/repos/{full_name}/issues"
            f"?state=all&since={since}&sort=created&direction=desc&per_page=50"
        )
        r = await client.get(url, headers=await self._gh_headers())
        if r.status_code != 200:
            _log.warning("poller: GET issues %s -> HTTP %s", full_name, r.status_code)
            return
        for it in r.json() if isinstance(r.json(), list) else []:
            if not isinstance(it, dict) or "pull_request" in it:
                continue  # PRs are handled by _poll_pulls
            num = it.get("number")
            created = _parse_iso(str(it.get("created_at") or ""))
            if num is None:
                continue
            state = str(it.get("state") or "open").casefold()

            # The `since` filter returns recently UPDATED issues, so state changes land here
            # too. Without state tracking the poller only ever emitted "opened" and closes /
            # reopens were invisible to the real-time path — tasks never reached Completed.
            # Sorge (PR #81): this state grew without bound over a long session. Cap the
            # per-repo maps; entries past the cap are the OLDEST issues, which the
            # baseline cutoff already excludes from re-processing.
            for key in ("issue_state", "issue_labels", "issue_text"):
                m2 = proc.get(key)
                if isinstance(m2, dict) and len(m2) > 2000:
                    for old in sorted(m2)[:500]:
                        m2.pop(old, None)
            issue_state = proc.setdefault("issue_state", {})
            prev = issue_state.get(num)
            issue_state[num] = state
            is_new = (
                created is not None and created >= baseline and num not in proc["issues_opened"]
            )

            # Labels land AFTER creation (issue #80: `bug` arrived 75s late; the task said
            # Story indefinitely). Track a metadata signature (type/priority/assignees/
            # labels) AND a text signature (title/body): sidebar priority changes and
            # post-creation edits must re-sync the task instead of freezing whatever the
            # creation-time race produced.
            meta_sig = issue_meta_signature(it)
            issue_labels = proc.setdefault("issue_labels", {})
            labels_prev = issue_labels.get(num)
            issue_labels[num] = meta_sig
            text_sig = issue_text_signature(it)
            issue_text = proc.setdefault("issue_text", {})
            text_prev = issue_text.get(num)
            issue_text[num] = text_sig

            action = ""
            if is_new:
                proc["issues_opened"].add(num)
                action = "opened"
            elif prev is not None and prev != state:
                action = "closed" if state == "closed" else "reopened"
            elif prev is None and num in proc["issues_opened"] and state == "closed":
                action = "closed"
            elif text_prev is not None and text_prev != text_sig:
                # "edited" syncs text AND metadata, so it wins when both changed.
                action = "edited"
            elif labels_prev is not None and labels_prev != meta_sig:
                action = "labeled"
            if not action:
                continue

            payload = IssueEventPayload(
                action=action,
                issue={
                    "number": it["number"],
                    "title": it.get("title") or "",
                    "body": it.get("body") or "",
                    "html_url": it.get("html_url") or "",
                    "state": state,
                    "user": it.get("user"),
                    "labels": it.get("labels") or [],
                    "type": it.get("type"),
                    "issue_field_values": it.get("issue_field_values") or [],
                    "assignees": it.get("assignees") or [],
                },
                repository={"full_name": full_name, "name": name},
            )
            result = await self._run_handler(payload)
            _log.info(
                "poller: issue #%s %s -> %s", num, action, (result or {}).get("message") or result
            )

    async def _poll_pulls(self, client, full_name, baseline, proc) -> None:
        url = (
            f"https://api.github.com/repos/{full_name}/pulls"
            f"?state=all&sort=updated&direction=desc&per_page=30"
        )
        r = await client.get(url, headers=await self._gh_headers())
        if r.status_code != 200:
            _log.warning("poller: GET pulls %s -> HTTP %s", full_name, r.status_code)
            return
        # Which PRs here still hold links a close retired. Asked once for the repo, because
        # the answer only changes when this loop changes it, and asking per PR meant a
        # database session for every open PR in every repo on every cycle.
        withdrawn_here = await self._withdrawn_pr_numbers(full_name.partition("/")[2])
        seen_here: set[int] = set()
        for pr in r.json() if isinstance(r.json(), list) else []:
            if not isinstance(pr, dict):
                continue
            updated = _parse_iso(str(pr.get("updated_at") or ""))
            if updated is not None and updated < baseline:
                break  # list is newest-updated first; the rest are older than baseline
            num = pr.get("number")
            if num is None:
                continue
            seen_here.add(int(num))
            created = _parse_iso(str(pr.get("created_at") or ""))
            if created is not None and created >= baseline and num not in proc["prs_opened"]:
                proc["prs_opened"].add(num)
                # Every PR the direct poll discovers is a REPLAY: `prs_opened` resets on
                # restart, so anything in the catch-up window is re-detected as fresh. A
                # task a QA moved to In QA within that window was dragged back to Needs QA
                # -- exactly the regression `is_replay` was added to prevent.
                result = await self._run_handler(
                    self._pr_payload(pr, full_name, "opened"), is_replay=True
                )
                _log.info(
                    "poller: PR #%s opened -> %s", num, (result or {}).get("message") or result
                )
            # draft -> ready_for_review (webhook-only event, derived from the draft flag)
            draft_now = bool(pr.get("draft"))
            draft_map = proc.setdefault("pr_draft", {})
            draft_prev = draft_map.get(num)
            draft_map[num] = draft_now
            if draft_prev is not None and draft_prev != draft_now and pr.get("state") == "open":
                act = "converted_to_draft" if draft_now else "ready_for_review"
                result = await self._run_handler(self._pr_payload(pr, full_name, act))
                _log.info(
                    "poller: PR #%s %s -> %s", num, act, (result or {}).get("message") or result
                )

            # label set changed (PRs have no native type — labels ARE their typing, and
            # people label after opening; mirror of the issue-side label tracking)
            labels_sig = tuple(
                sorted(
                    str((lb or {}).get("name") or "")
                    for lb in (pr.get("labels") or [])
                    if isinstance(lb, dict)
                )
            )
            pr_labels = proc.setdefault("pr_labels", {})
            labels_prev = pr_labels.get(num)
            pr_labels[num] = labels_sig
            if labels_prev is not None and labels_prev != labels_sig:
                result = await self._run_handler(self._pr_payload(pr, full_name, "labeled"))
                _log.info(
                    "poller: PR #%s labeled %s -> %s",
                    num,
                    list(labels_sig),
                    (result or {}).get("message") or result,
                )

            # newly requested reviewers (webhook-only event, derived from requested_reviewers)
            head_ref = str(((pr.get("head") or {}) or {}).get("ref") or "").strip()
            track_pr_branch(
                proc.setdefault("pr_branches", {}),
                num,
                head_ref,
                str(pr.get("state") or ""),
            )

            reviewers_now = {
                str((u or {}).get("login") or "").strip()
                for u in (pr.get("requested_reviewers") or [])
                if isinstance(u, dict) and (u or {}).get("login")
            }
            rev_map = proc.setdefault("pr_reviewers", {})
            reviewers_prev = rev_map.get(num)
            rev_map[num] = reviewers_now
            if reviewers_prev is not None and (reviewers_now - reviewers_prev):
                added = sorted(reviewers_now - reviewers_prev)
                result = await self._run_handler(
                    self._pr_payload(pr, full_name, "review_requested")
                )
                _log.info(
                    "poller: PR #%s review_requested %s -> %s",
                    num,
                    added,
                    (result or {}).get("message") or result,
                )

            if pr.get("state") == "open" and (num in proc["prs_closed"] or num in withdrawn_here):
                # Closed, then open again. Closing withdrew this PR's links, so without a
                # reopened event every later review, comment and push resolves to no task
                # at all -- permanently, since nothing else clears the flag.
                #
                # The in-process set alone was not enough: it is empty after a restart, so
                # a PR reopened while this was down emitted nothing. The registry remembers
                # what the set forgets, and once the links are revived the condition stops
                # being true, so this fires once rather than every cycle.
                proc["prs_closed"].discard(num)
                result = await self._run_handler(self._pr_payload(pr, full_name, "reopened"))
                _log.info(
                    "poller: PR #%s reopened -> %s", num, (result or {}).get("message") or result
                )

            if pr.get("state") == "closed" and num not in proc["prs_closed"]:
                proc["prs_closed"].add(num)
                merged = bool(pr.get("merged_at"))
                result = await self._run_handler(
                    self._pr_payload(pr, full_name, "closed", merged=merged)
                )
                _log.info(
                    "poller: PR #%s %s -> %s",
                    num,
                    "merged" if merged else "closed",
                    (result or {}).get("message") or result,
                )

        # The listing is newest-updated first and stops at the baseline, so a PR reopened
        # while this was down longer than the catch-up window is never reached by the loop
        # above -- which is exactly the case the withdrawn-link recovery exists for. Ask
        # about those by number instead. There are normally none, they stop being withdrawn
        # once handled, and the per-cycle cap keeps a strange backlog from spending the
        # whole API budget on one repo.
        probed = proc.setdefault("reopen_probed", set())
        for num in sorted(withdrawn_here - seen_here - probed)[:_MAX_REOPEN_PROBES_PER_CYCLE]:
            probe = await client.get(
                f"https://api.github.com/repos/{full_name}/pulls/{num}",
                headers=await self._gh_headers(),
            )
            if probe.status_code != 200:
                continue
            pr = probe.json()
            if not isinstance(pr, dict) or pr.get("state") != "open":
                # Closed, and staying closed. Nothing ever clears `withdrawn_at` for one of
                # those, so without remembering the answer the oldest few took the whole
                # probe budget every cycle -- permanently, and outside what
                # `cycle_call_estimate` prices -- while a PR that HAD reopened was never
                # reached. Per process, so a restart asks once more, which is when it
                # matters.
                probed.add(int(num))
                continue
            proc["prs_closed"].discard(num)
            result = await self._run_handler(self._pr_payload(pr, full_name, "reopened"))
            _log.info(
                "poller: PR #%s was reopened while we were not watching -> %s",
                num,
                (result or {}).get("message") or result,
            )

    def _pr_payload(
        self, pr: dict, full_name: str, action: str, *, merged: bool = False
    ) -> PullRequestEventPayload:
        name = full_name.partition("/")[2]
        prd = dict(pr)
        # REST list omits the boolean `merged`; derive it from merged_at.
        prd["merged"] = merged or bool(pr.get("merged_at")) or bool(pr.get("merged"))
        return PullRequestEventPayload(
            action=action,
            pull_request=prd,
            repository={"full_name": full_name, "name": name},
        )

    async def _poll_commits(self, client, full_name, since, proc) -> None:
        """Poll the default branch AND every open PR's head branch.

        GET /commits without `sha` returns only the default branch. Real work happens on
        feature branches, so polling just the default meant commit->task comments almost
        never fired outside of direct-to-main pushes. SHA dedupe makes the overlap free.
        """
        branches: list[str] = [""]  # "" = repo default branch
        branches += sorted(set((proc.get("pr_branches") or {}).values()))

        normalized: list[dict] = []
        actor = ""
        for branch in branches:
            url = f"https://api.github.com/repos/{full_name}/commits?since={since}&per_page=30"
            if branch:
                from urllib.parse import quote

                url += f"&sha={quote(branch, safe='')}"
            r = await client.get(url, headers=await self._gh_headers())
            if r.status_code != 200:
                continue
            commits = r.json()
            if not isinstance(commits, list):
                continue
            for c in commits:
                if not isinstance(c, dict):
                    continue
                sha = str(c.get("sha") or "")
                if not sha or sha in proc["commits"]:
                    continue
                proc["commits"].add(sha)
                actor = ((c.get("author") or {}) or {}).get("login") or actor
                normalized.append(
                    {
                        "sha": sha,
                        "message": str((c.get("commit") or {}).get("message") or ""),
                    }
                )
        if normalized:
            await self._comment_commits(full_name, actor or "someone", normalized)

    async def _poll_repo(self, full_name: str) -> None:
        if not github_auth_available():
            _log.warning("poller: GITHUB_PAT missing — cannot poll %s", full_name)
            return
        headers = {}
        etag = self._etags.get(full_name)
        if etag:
            headers["If-None-Match"] = etag
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{full_name}/events?per_page=50",
                headers={**(await github_auth_header()), **headers},
            )
        if r.status_code == 304:
            return
        if r.status_code != 200:
            _log.warning("poller: GET /repos/%s/events -> HTTP %s", full_name, r.status_code)
            return
        if "ETag" in r.headers:
            self._etags[full_name] = r.headers["ETag"]
        events = r.json()
        if not isinstance(events, list) or not events:
            return

        def _eid(e: dict[str, Any]) -> str:
            return str(e.get("id") or "")

        def _etime(e: dict[str, Any]) -> str:
            return str(e.get("created_at") or "")

        seen = self._seen_ids.get(full_name)
        if seen is None:
            # First poll after startup: baseline everything visible so pre-start history is not
            # replayed. Also process events created within the catch-up window so a restart does
            # not drop activity that happened while the machine was (or should have been) running.
            seen = {_eid(e) for e in events if _eid(e)}
            self._seen_ids[full_name] = seen
            catchup = max(0.0, float(settings.testing_live_plaky_catchup_minutes or 0.0))
            fresh: list[dict[str, Any]] = []
            if catchup > 0:
                cutoff = datetime.now(UTC) - timedelta(minutes=catchup)
                for e in events:
                    dt = _parse_iso(_etime(e))
                    if dt is not None and dt >= cutoff:
                        fresh.append(e)
            if fresh:
                _log.info(
                    "poller: %s baseline set (%d events); catching up %d event(s) from the last %.0f min",
                    full_name,
                    len(events),
                    len(fresh),
                    catchup,
                )
            else:
                _log.info(
                    "poller: %s baseline set (%d events; history not replayed)",
                    full_name,
                    len(events),
                )
                return
        else:
            fresh = [e for e in events if _eid(e) and _eid(e) not in seen]
            for e in events:
                if _eid(e):
                    seen.add(_eid(e))
            # Bound memory: aged-out events never reappear in the feed, so collapsing to the
            # current window is safe.
            if len(seen) > 1500:
                self._seen_ids[full_name] = {_eid(e) for e in events if _eid(e)}
            if not fresh:
                return

        # Oldest first (by event time) so Plaky sees actions in order. Issues, PRs, and pushes
        # come from the real-time direct poll; the events feed only covers reviews and comments
        # here (no simple real-time "since" list exists for those).
        for event in sorted(fresh, key=_etime):
            if str(event.get("type")) not in EVENTS_FEED_TYPES:
                continue
            try:
                await self._dispatch_event(full_name, event)
            except Exception:  # noqa: BLE001 - observability failure must not affect the request
                _log.exception(
                    "poller: failed handling %s event %s", event.get("type"), event.get("id")
                )

    async def _dispatch_event(self, full_name: str, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        if etype == "PushEvent":
            await self._handle_push(full_name, event, payload)
            return

        webhook_event = _EVENT_TYPE_TO_WEBHOOK.get(etype)
        if not webhook_event:
            return

        # Events-feed payloads match webhook payloads except `repository` is at the
        # envelope level, and reviews arrive with action "created" instead of "submitted".
        owner, _, short = full_name.partition("/")
        payload_dict = dict(payload)
        payload_dict["repository"] = {"full_name": full_name, "name": short}
        if etype == "PullRequestReviewEvent" and payload_dict.get("action") == "created":
            payload_dict["action"] = "submitted"

        parsed = parse_webhook_payload(webhook_event, payload_dict)
        if parsed is None:
            return
        result = await self._run_handler(parsed)
        if result is not None:
            _log.info(
                "poller: %s %s on %s -> %s",
                etype,
                payload_dict.get("action", ""),
                full_name,
                result.get("message") or result.get("action") or result,
            )

    async def _withdrawn_pr_numbers(self, repo_short: str) -> set[int]:
        """PRs in this repo still holding retired links. One query per cycle, not per PR."""
        from boardman.services.pr_task_registry import withdrawn_pr_numbers

        try:
            async with async_session() as session:
                return await withdrawn_pr_numbers(session, github_repo=repo_short)
        except Exception as exc:  # noqa: BLE001 - a poll must not die on a DB blip
            log_degraded(_log, f"poller: reading withdrawn links for {repo_short}", exc)
            return set()

    async def _run_handler(self, parsed: Any, *, is_replay: bool = False) -> dict[str, Any] | None:
        """Mirror of the dispatch in routes/github_events.py, with a poller-owned DB session."""
        async with async_session() as session:
            try:
                result: dict[str, Any] | None = None
                if isinstance(parsed, IssueEventPayload):
                    if parsed.action == "opened":
                        result = await handle_issue_opened(parsed, session)
                    elif parsed.action == "closed":
                        from boardman.services.issue_handler import handle_issue_closed

                        result = await handle_issue_closed(parsed, session)
                    elif parsed.action == "reopened":
                        from boardman.services.issue_handler import handle_issue_reopened

                        result = await handle_issue_reopened(parsed, session)
                    elif parsed.action == "edited":
                        from boardman.services.issue_handler import handle_issue_edited

                        result = await handle_issue_edited(parsed, session)
                    elif parsed.action in (
                        "labeled",
                        "unlabeled",
                        "assigned",
                        "unassigned",
                        "typed",
                        "untyped",
                    ):
                        from boardman.services.issue_handler import handle_issue_labels_changed

                        result = await handle_issue_labels_changed(parsed, session)
                elif isinstance(parsed, PullRequestReviewEventPayload):
                    result = await handle_pull_request_review(parsed, session)
                elif isinstance(parsed, PullRequestReviewCommentEventPayload):
                    # Same actions the webhook route accepts, so a poller-driven local
                    # run and production see identical events.
                    if parsed.action in ("created", "edited"):
                        result = await handle_pr_review_comment(parsed, session)
                elif isinstance(parsed, IssueCommentEventPayload):
                    result = await handle_issue_comment_on_pr(parsed, session)
                elif isinstance(parsed, PullRequestEventPayload):
                    if parsed.action in ("opened", "reopened"):
                        # An `opened` from the events feed after a restart is a replay,
                        # not a PR arriving for the first time. A `reopened` is a real
                        # event and should be handled as one.
                        result = await handle_pr_opened(
                            parsed, session, is_replay=parsed.action == "opened"
                        )
                    elif parsed.action == "ready_for_review":
                        from boardman.services.pr_handler import handle_pr_ready_for_review

                        result = await handle_pr_ready_for_review(parsed, session)
                    elif parsed.action == "review_requested":
                        from boardman.services.pr_handler import handle_pr_review_requested

                        result = await handle_pr_review_requested(parsed, session)
                    elif parsed.action in ("labeled", "unlabeled"):
                        from boardman.services.pr_handler import handle_pr_labels_changed

                        result = await handle_pr_labels_changed(parsed, session)
                    elif parsed.action == "edited":
                        from boardman.services.pr_handler import handle_pr_edited

                        result = await handle_pr_edited(parsed, session)
                    elif parsed.action == "converted_to_draft":
                        from boardman.services.pr_handler import handle_pr_converted_to_draft

                        result = await handle_pr_converted_to_draft(parsed, session)
                    elif parsed.action == "closed" and parsed.pull_request.merged:
                        result = await handle_pr_merged(parsed, session)
                    elif parsed.action == "closed":
                        result = await handle_pr_closed_without_merge(parsed, session)
                await session.commit()
                # The poller does NOT route through dispatch_github_event, so it needs
                # its own line here. Without it, every event in a live TESTING_LIVE_PLAKY
                # session leaves stale repo context behind while the webhook path looks
                # correct in tests.
                note_repo_changed(repo_full_name_from_payload(parsed))
                return result
            except Exception:  # noqa: BLE001 - GitHub API failure degrades gracefully
                await session.rollback()
                raise

    async def _handle_push(
        self, full_name: str, event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Events-feed PushEvent path (kept for completeness/tests). Delegates to _comment_commits."""
        commits = payload.get("commits")
        if not isinstance(commits, list) or not commits:
            return
        actor = ((event.get("actor") or {}).get("login")) or "someone"
        normalized = [
            {"sha": str(c.get("sha") or ""), "message": str(c.get("message") or "")}
            for c in commits
            if isinstance(c, dict)
        ]
        await self._comment_commits(full_name, actor, normalized)

    async def _comment_commits(self, full_name: str, actor: str, commits: list[dict]) -> None:
        """Comment commits onto Plaky tasks linked to issues their messages reference."""
        _, _, short = full_name.partition("/")
        from boardman.plaky.client import PlakyClient

        async with async_session() as session:
            plaky = PlakyClient()
            for c in commits[:20]:
                message = str(c.get("message") or "")
                sha = str(c.get("sha") or "")
                issue_numbers = {int(m.group(1)) for m in _COMMIT_ISSUE_RE.finditer(message)}
                if not issue_numbers or not sha:
                    continue
                first_line = message.splitlines()[0][:200] if message else ""
                url = f"https://github.com/{full_name}/commit/{sha}"
                for num in sorted(issue_numbers):
                    mapping = await find_plaky_task_by_issue(short, num, session)
                    if not mapping:
                        continue
                    marker = f"{sha}:{num}"
                    if await comment_already_synced(session, "commit_comment_synced", marker):
                        continue
                    if settings.plaky_pr_comment_links_as_html:
                        body = f'Commit by @{actor}: {first_line} (<a href="{url}">{sha[:7]}</a>)'
                    else:
                        body = f"Commit by @{actor}: {first_line} ({url})"
                    res = await plaky.add_comment(mapping.plaky_task_id, body)
                    # A refused post left nothing on the card, so the row must not claim
                    # the identity that dedupes it: written under the real action, one
                    # transient Plaky error suppressed this commit comment for good. The
                    # attempt is still recorded, under an action nothing matches against.
                    posted = bool(res.get("ok"))
                    session.add(
                        SyncLog(
                            action=("commit_comment_synced" if posted else "commit_comment_failed"),
                            github_repo=short,
                            github_ref=str(num),
                            plaky_task_id=mapping.plaky_task_id,
                            detail=json.dumps(
                                {"marker": marker, "commit_url": url, "plaky_ok": posted},
                                default=str,
                            ),
                        )
                    )
                    _log.info(
                        "poller: commit %s -> comment on Plaky task %s (issue #%s): ok=%s",
                        sha[:7],
                        mapping.plaky_task_id,
                        num,
                        (res or {}).get("ok"),
                    )
            await session.commit()
            # A push is the one event that changes the code itself, so the tree, file
            # and hotspot reads are the ones that must go.
            note_repo_changed(full_name, event="push")


_poller: GitHubEventPoller | None = None


def start_github_poller_if_enabled() -> GitHubEventPoller | None:
    """Start the poller when TESTING_LIVE_PLAKY is on. Called from the app lifespan."""
    global _poller
    if not settings.testing_live_plaky:
        return None
    if not poller_repos() and not watch_all_requested():
        _log.warning(
            "TESTING_LIVE_PLAKY=true but TESTING_LIVE_PLAKY_REPOS is empty — poller not "
            "started. Set it to a comma-separated owner/repo list, or to `all` to watch "
            "every non-archived repo that resolves to a Plaky board."
        )
        return None
    if _poller is None:
        _poller = GitHubEventPoller()
    _poller.start()
    return _poller


async def stop_github_poller() -> None:
    global _poller
    if _poller is not None:
        await _poller.stop()
        _poller = None
