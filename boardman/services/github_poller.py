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
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from boardman.database.session import async_session
from boardman.github.webhooks import (
    IssueCommentEventPayload,
    IssueEventPayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
    parse_webhook_payload,
)
from boardman.services.issue_handler import handle_issue_opened, find_plaky_task_by_issue
from boardman.services.pr_handler import (
    handle_pr_closed_without_merge,
    handle_pr_merged,
    handle_pr_opened,
    handle_pr_review_comment,
)
from boardman.services.pr_review_handler import handle_issue_comment_on_pr, handle_pull_request_review
from boardman.settings import settings

_log = logging.getLogger(__name__)

# Commit messages referencing issues: "Fixes #12", "closes #3", or a bare "#12".
_COMMIT_ISSUE_RE = re.compile(r"(?:(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+)?#(\d+)", re.IGNORECASE)


def _parse_iso(value: str) -> Optional[datetime]:
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
        dt = dt.replace(tzinfo=timezone.utc)
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


def poller_repos() -> list[str]:
    out: list[str] = []
    for chunk in (settings.testing_live_plaky_repos or "").replace("\n", ",").split(","):
        s = chunk.strip()
        if s and "/" in s and s not in out:
            out.append(s)
    return out


class GitHubEventPoller:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
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

    def _gh_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.github_pat}",
            "Accept": "application/vnd.github+json",
        }

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
        interval = max(15.0, float(settings.testing_live_plaky_poll_seconds or 60.0))
        repos = poller_repos()
        _log.info(
            "TESTING_LIVE_PLAKY: GitHub poller started — repos=%s interval=%.0fs "
            "(Plaky updates apply only while this instance runs)",
            repos,
            interval,
        )
        while not self._stop.is_set():
            for repo in repos:
                # Real-time REST endpoints (no events-feed lag) for the creation/push actions,
                # then the events feed for reviews + comments (which have no simple "since" list).
                try:
                    await self._poll_direct(repo)
                except httpx.HTTPError as e:
                    _log.warning("poller: direct poll of %s failed (transient network): %s", repo, e)
                except Exception:
                    _log.exception("poller: unexpected error in direct poll of %s", repo)
                try:
                    await self._poll_repo(repo)
                except httpx.HTTPError as e:
                    _log.warning("poller: events poll of %s failed (transient network): %s", repo, e)
                except Exception:
                    _log.exception("poller: unexpected error polling events of %s", repo)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        _log.info("TESTING_LIVE_PLAKY: GitHub poller stopped")

    # ── Real-time direct polling (issues / PRs / commits) ────────────────────────
    async def _poll_direct(self, full_name: str) -> None:
        if not (settings.github_pat or "").strip():
            return
        baseline = self._baseline_dt.get(full_name)
        if baseline is None:
            catchup = max(0.0, float(settings.testing_live_plaky_catchup_minutes or 0.0))
            baseline = datetime.now(timezone.utc) - timedelta(minutes=catchup)
            self._baseline_dt[full_name] = baseline
            self._processed[full_name] = {
                "issues_opened": set(),
                "prs_opened": set(),
                "prs_closed": set(),
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
                    _log.warning("poller: %s poll of %s failed (transient network): %s", label, full_name, e)
                except Exception:
                    _log.exception("poller: %s poll of %s errored", label, full_name)

    async def _poll_issues(self, client, full_name, baseline, since, proc) -> None:
        owner, _, name = full_name.partition("/")
        url = (
            f"https://api.github.com/repos/{full_name}/issues"
            f"?state=all&since={since}&sort=created&direction=desc&per_page=50"
        )
        r = await client.get(url, headers=self._gh_headers())
        if r.status_code != 200:
            _log.warning("poller: GET issues %s -> HTTP %s", full_name, r.status_code)
            return
        for it in r.json() if isinstance(r.json(), list) else []:
            if not isinstance(it, dict) or "pull_request" in it:
                continue  # PRs are handled by _poll_pulls
            num = it.get("number")
            created = _parse_iso(str(it.get("created_at") or ""))
            if num is None or created is None or created < baseline:
                continue
            if num in proc["issues_opened"]:
                continue
            proc["issues_opened"].add(num)
            payload = IssueEventPayload(
                action="opened",
                issue={
                    "number": it["number"],
                    "title": it.get("title") or "",
                    "body": it.get("body") or "",
                    "html_url": it.get("html_url") or "",
                    "state": it.get("state") or "open",
                    "user": it.get("user"),
                },
                repository={"full_name": full_name, "name": name},
            )
            result = await self._run_handler(payload)
            _log.info("poller: issue #%s opened -> %s", num, (result or {}).get("message") or result)

    async def _poll_pulls(self, client, full_name, baseline, proc) -> None:
        name = full_name.partition("/")[2]
        url = (
            f"https://api.github.com/repos/{full_name}/pulls"
            f"?state=all&sort=updated&direction=desc&per_page=30"
        )
        r = await client.get(url, headers=self._gh_headers())
        if r.status_code != 200:
            _log.warning("poller: GET pulls %s -> HTTP %s", full_name, r.status_code)
            return
        for pr in r.json() if isinstance(r.json(), list) else []:
            if not isinstance(pr, dict):
                continue
            updated = _parse_iso(str(pr.get("updated_at") or ""))
            if updated is not None and updated < baseline:
                break  # list is newest-updated first; the rest are older than baseline
            num = pr.get("number")
            if num is None:
                continue
            created = _parse_iso(str(pr.get("created_at") or ""))
            if created is not None and created >= baseline and num not in proc["prs_opened"]:
                proc["prs_opened"].add(num)
                result = await self._run_handler(self._pr_payload(pr, full_name, "opened"))
                _log.info("poller: PR #%s opened -> %s", num, (result or {}).get("message") or result)
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

    def _pr_payload(self, pr: dict, full_name: str, action: str, *, merged: bool = False) -> PullRequestEventPayload:
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
        url = f"https://api.github.com/repos/{full_name}/commits?since={since}&per_page=30"
        r = await client.get(url, headers=self._gh_headers())
        if r.status_code != 200:
            return
        commits = r.json()
        if not isinstance(commits, list):
            return
        normalized: list[dict] = []
        actor = ""
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
        if not (settings.github_pat or "").strip():
            _log.warning("poller: GITHUB_PAT missing — cannot poll %s", full_name)
            return
        headers = {}
        etag = self._etags.get(full_name)
        if etag:
            headers["If-None-Match"] = etag
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{full_name}/events?per_page=50",
                headers={
                    "Authorization": f"Bearer {settings.github_pat}",
                    "Accept": "application/vnd.github+json",
                    **headers,
                },
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
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=catchup)
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
            except Exception:
                _log.exception("poller: failed handling %s event %s", event.get("type"), event.get("id"))

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

    async def _run_handler(self, parsed: Any) -> Optional[dict[str, Any]]:
        """Mirror of the dispatch in routes/github_events.py, with a poller-owned DB session."""
        async with async_session() as session:
            try:
                result: Optional[dict[str, Any]] = None
                if isinstance(parsed, IssueEventPayload):
                    if parsed.action == "opened":
                        result = await handle_issue_opened(parsed, session)
                    elif parsed.action == "closed":
                        from boardman.services.issue_handler import handle_issue_closed

                        result = await handle_issue_closed(parsed, session)
                    elif parsed.action == "reopened":
                        from boardman.services.issue_handler import handle_issue_reopened

                        result = await handle_issue_reopened(parsed, session)
                elif isinstance(parsed, PullRequestReviewEventPayload):
                    result = await handle_pull_request_review(parsed, session)
                elif isinstance(parsed, PullRequestReviewCommentEventPayload):
                    if parsed.action == "created":
                        result = await handle_pr_review_comment(parsed, session)
                elif isinstance(parsed, IssueCommentEventPayload):
                    result = await handle_issue_comment_on_pr(parsed, session)
                elif isinstance(parsed, PullRequestEventPayload):
                    if parsed.action in ("opened", "reopened"):
                        result = await handle_pr_opened(parsed, session)
                    elif parsed.action == "ready_for_review":
                        from boardman.services.pr_handler import handle_pr_ready_for_review

                        result = await handle_pr_ready_for_review(parsed, session)
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
                return result
            except Exception:
                await session.rollback()
                raise

    async def _handle_push(self, full_name: str, event: dict[str, Any], payload: dict[str, Any]) -> None:
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
                    if settings.plaky_pr_comment_links_as_html:
                        body = f'Commit by @{actor}: {first_line} (<a href="{url}">{sha[:7]}</a>)'
                    else:
                        body = f"Commit by @{actor}: {first_line} ({url})"
                    res = await plaky.add_comment(mapping.plaky_task_id, body)
                    _log.info(
                        "poller: commit %s -> comment on Plaky task %s (issue #%s): ok=%s",
                        sha[:7],
                        mapping.plaky_task_id,
                        num,
                        (res or {}).get("ok"),
                    )
            await session.commit()


_poller: Optional[GitHubEventPoller] = None


def start_github_poller_if_enabled() -> Optional[GitHubEventPoller]:
    """Start the poller when TESTING_LIVE_PLAKY is on. Called from the app lifespan."""
    global _poller
    if not settings.testing_live_plaky:
        return None
    if not poller_repos():
        _log.warning("TESTING_LIVE_PLAKY=true but TESTING_LIVE_PLAKY_REPOS is empty — poller not started")
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
