"""Once a PR is merged or closed, Boardman must not start new @mention/comment work on it.

Live failure (reported 2026-09-26, deepiri-mudspeed#50): the PR merged at
2026-09-14T20:36Z, and `deepiri-boardman[bot]` went on commenting

    ⏰ This PR has been waiting on QA review for 3 day(s) ... @sergiovargas111
    ⏰ ... 6 day(s) ... ⏰ ... 9 day(s) ... ⏰ ... 12 day(s) ...

on 09-17, 09-20, 09-23 and 09-26 — after day 15 it would be every day, forever.

Two things combined, and both are covered here:

1. `handle_pr_merged` / `handle_pr_closed_without_merge` left the `pr_review_nudges`
   row behind. Nothing in the event stream ever removed it, so "waiting on QA" outlived
   the PR it was about. (`test_merge_event_retires_the_nudge_row` et al.)
2. Even with the row gone at merge time, the sweep is a periodic loop that can run
   before the merge delivery arrives, or after a delivery that never came at all. It
   read only its own table, so it had no way to notice the PR had finished.
   (`test_sweep_asks_github_before_mentioning_anyone`)

The invariant is enforced from the AUTHORITATIVE state, not the event payload: a
`pull_request` payload is a photograph from when GitHub emitted it, and a delivery that
sat in the job queue, a retry after a 500, or the hourly sweep all act on it later.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember
from boardman.database.models import Base, PrReviewNudge, PullRequestTaskLink
from boardman.github import pr_actions
from boardman.github.webhooks import GitHubPullRequest, GitHubRepository, PullRequestEventPayload
from boardman.services import pr_handler as ph
from boardman.services import pr_review_nudges as nudges

REPO = "deepiri-mudspeed"
FULL = f"Team-Deepiri/{REPO}"
PR = 50
QA = "sergiovargas111"
DEV = "connorwhite9"


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- pr_lifecycle_state: the tri-state contract ----------------------------------------


class _Resp:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload


class _Client:
    def __init__(self, resp: _Resp | Exception) -> None:
        self._resp = resp
        self.urls: list[str] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, url: str, **_kw: Any) -> _Resp:
        self.urls.append(url)
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _probe(monkeypatch: pytest.MonkeyPatch, resp: _Resp | Exception) -> _Client:
    """Run the probe against a canned GitHub response, with auth switched on."""

    async def _headers() -> dict[str, str]:
        return {"Authorization": "Bearer test"}

    client = _Client(resp)
    monkeypatch.setattr(pr_actions, "github_auth_available", lambda: True)
    monkeypatch.setattr(pr_actions, "github_auth_header", _headers)
    monkeypatch.setattr(pr_actions, "shared_github_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_open_pr_reads_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _probe(monkeypatch, _Resp(200, {"state": "open", "merged": False}))
    assert await pr_actions.pr_lifecycle_state(FULL, PR) == pr_actions.PR_ACTIVE
    # The single-PR GET, not the list endpoint: only this one carries `merged`
    # (see the note in reconcile.py about what reading it from `pulls?state=all` cost).
    assert client.urls == [f"https://api.github.com/repos/{FULL}/pulls/{PR}"]


@pytest.mark.asyncio
async def test_merged_pr_reads_as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, _Resp(200, {"state": "closed", "merged": True}))
    assert await pr_actions.pr_lifecycle_state(FULL, PR) == pr_actions.PR_TERMINAL


@pytest.mark.asyncio
async def test_closed_unmerged_pr_reads_as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, _Resp(200, {"state": "closed", "merged": False}))
    assert await pr_actions.pr_lifecycle_state(FULL, PR) == pr_actions.PR_TERMINAL


@pytest.mark.asyncio
async def test_unreachable_github_reads_as_unknown_not_as_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unknown` must stay distinct from `active`: this gates an @mention, and an
    unreadable PR is not a reason to assume the best. A skipped nudge is retried on the
    next sweep; a mention on a shipped PR cannot be taken back."""
    for failure in (500, 404, RuntimeError("connection reset")):
        _probe(monkeypatch, failure if isinstance(failure, Exception) else _Resp(failure))
        assert await pr_actions.pr_lifecycle_state(FULL, PR) == pr_actions.PR_STATE_UNKNOWN


@pytest.mark.asyncio
async def test_no_github_credential_reads_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_actions, "github_auth_available", lambda: False)
    assert await pr_actions.pr_lifecycle_state(FULL, PR) == pr_actions.PR_STATE_UNKNOWN


# --- the sweep asks GitHub before it mentions anyone ------------------------------------


async def _tracked_but_stale(db_session) -> None:
    """Exactly what `_assign_qa_for_pr` leaves behind on a PR that is about to go quiet."""
    await nudges.ensure_tracked(
        db_session,
        github_repo=REPO,
        github_pr_number=PR,
        developer_login=DEV,
        primary_qa_login=QA,
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, REPO, PR)
    row.last_activity_at = datetime.utcnow() - timedelta(days=3)
    await db_session.commit()


def _merge_payload(*, merged: bool = True, state: str = "closed") -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action="closed",
        pull_request=GitHubPullRequest(
            number=PR,
            title="test(bridge): live collector→adapter drift guard (phase 4)",
            html_url=f"https://github.com/{FULL}/pull/{PR}",
            state=state,
            merged=merged,
            draft=False,
            body="",
            user={"login": DEV},
        ),
        repository=GitHubRepository(full_name=FULL, name=REPO),
    )


@pytest.mark.asyncio
async def test_sweep_asks_github_before_mentioning_anybody(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's DB says "waiting on QA". Only GitHub says whether that is still true."""
    await _tracked_but_stale(db_session)

    posted: list[tuple] = []
    state = {"value": pr_actions.PR_TERMINAL}

    async def fake_state(full_name: str, pr_number: int) -> str:
        posted.append(("state-read", full_name, pr_number))
        return state["value"]

    async def fake_comment(full_name: str, pr_number: int, body: str) -> dict[str, Any]:
        posted.append(("comment", full_name, pr_number, body))
        return {"ok": True}

    monkeypatch.setattr(nudges, "pr_lifecycle_state", fake_state)
    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)

    merged = await nudges.sweep_due_nudges(db_session)
    assert [k for k, *_ in posted] == ["state-read"]
    assert merged[0]["skipped"] == "pr_not_active"

    # Flip the same row back to a live PR: the identical sweep now mentions them.
    state["value"] = pr_actions.PR_ACTIVE
    await nudges.ensure_tracked(
        db_session,
        github_repo=REPO,
        github_pr_number=PR,
        developer_login=DEV,
        primary_qa_login=QA,
    )
    await db_session.commit()
    row = await nudges._get_row(db_session, REPO, PR)
    row.last_activity_at = datetime.utcnow() - timedelta(days=3)
    await db_session.commit()

    live = await nudges.sweep_due_nudges(db_session)
    assert "skipped" not in live[0]
    assert [k for k, *_ in posted] == ["state-read", "state-read", "comment"]
    assert f"@{QA}" in posted[-1][3]


@pytest.mark.asyncio
async def test_an_unreadable_state_never_becomes_a_mention(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on the outward action, fail open on the bookkeeping.

    No comment, and the escalation stage is left alone so the next sweep still tries —
    retiring the row here would silently abandon a PR that is still open just because
    GitHub was briefly unreachable.
    """
    await _tracked_but_stale(db_session)

    async def fake_state(_f: str, _p: int) -> str:
        return pr_actions.PR_STATE_UNKNOWN

    async def boom(*_a: Any, **_kw: Any):
        raise AssertionError("must not @mention anyone on an unreadable PR state")

    monkeypatch.setattr(nudges, "pr_lifecycle_state", fake_state)
    monkeypatch.setattr(nudges, "comment_on_pr", boom)

    assert await nudges.sweep_due_nudges(db_session) == []
    row = await nudges._get_row(db_session, REPO, PR)
    assert row is not None and row.nudge_stage == 0


# --- the merge/close events retire the row ----------------------------------------------


@pytest.fixture()
def plaky_stub(monkeypatch: pytest.MonkeyPatch):
    """Both merge handlers talk to Plaky; none of that matters for the nudge row."""

    class FakePlaky:
        async def add_comment(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            return {"ok": True}

    async def fake_status(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"ok": True}

    async def no_open_prs(*_a: Any, **_kw: Any) -> bool:
        return False

    async def fake_routing(*_a: Any, **_kw: Any):
        class Routing:
            plaky_board_id = "269031"
            plaky_group_id = "g1"

        return Routing()

    monkeypatch.setattr(ph, "PlakyClient", lambda *_a, **_kw: FakePlaky())
    monkeypatch.setattr(ph, "_update_plaky_task_status", fake_status)
    monkeypatch.setattr(ph, "has_any_open_pr_for_task", no_open_prs)
    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)


@pytest.mark.asyncio
async def test_merge_event_retires_the_nudge_row(db_session, plaky_stub) -> None:
    """Event-driven half: the row normally goes away when the PR does."""
    await _tracked_but_stale(db_session)
    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=PR,
            github_issue_number=0,
            plaky_task_id="task-50",
            link_source="auto_link",
        )
    )
    await db_session.commit()

    out = await ph.handle_pr_merged(_merge_payload(), db_session)
    assert out.get("ok") is True
    assert await nudges._get_row(db_session, REPO, PR) is None


@pytest.mark.asyncio
async def test_close_without_merge_retires_the_nudge_row(db_session, plaky_stub) -> None:
    """An abandoned PR is terminal for this purpose too — nobody can act on that review."""
    await _tracked_but_stale(db_session)

    out = await ph.handle_pr_closed_without_merge(
        _merge_payload(merged=False, state="closed"), db_session
    )
    assert out.get("ok") is True
    assert await nudges._get_row(db_session, REPO, PR) is None


@pytest.mark.asyncio
async def test_a_merge_delivery_that_never_arrives_is_still_caught_by_the_sweep(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the event half cannot cover on its own.

    The sweep can reach a due row before the queued `pull_request.closed` job runs, or
    the delivery can be lost outright. Both look identical from the table, so the sweep
    has to ask GitHub.
    """
    await _tracked_but_stale(db_session)

    async def terminal(_f: str, _p: int) -> str:
        return pr_actions.PR_TERMINAL

    async def boom(*_a: Any, **_kw: Any):
        raise AssertionError("merged PR must not be mentioned, delivery or no delivery")

    monkeypatch.setattr(nudges, "pr_lifecycle_state", terminal)
    monkeypatch.setattr(nudges, "comment_on_pr", boom)

    results = await nudges.sweep_due_nudges(db_session)
    assert results[0]["skipped"] == "pr_not_active"
    assert await nudges._get_row(db_session, REPO, PR) is None


@pytest.mark.asyncio
async def test_retiring_one_prs_row_leaves_others_alone(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two PRs waiting on QA, one merged. The open one still gets its escalation."""
    for number in (50, 51):
        await nudges.ensure_tracked(
            db_session,
            github_repo=REPO,
            github_pr_number=PR if number == 50 else 51,
            developer_login=DEV,
            primary_qa_login=QA,
        )
    await db_session.commit()
    for number in (50, 51):
        row = await nudges._get_row(db_session, REPO, number)
        row.last_activity_at = datetime.utcnow() - timedelta(days=3)
    await db_session.commit()

    async def fake_state(_full: str, pr_number: int) -> str:
        return pr_actions.PR_TERMINAL if pr_number == 50 else pr_actions.PR_ACTIVE

    posted: list[int] = []

    async def fake_comment(_full: str, pr_number: int, _body: str) -> dict[str, Any]:
        posted.append(pr_number)
        return {"ok": True}

    monkeypatch.setattr(nudges, "pr_lifecycle_state", fake_state)
    monkeypatch.setattr(nudges, "comment_on_pr", fake_comment)

    results = await nudges.sweep_due_nudges(db_session)
    assert posted == [51]
    assert [(r["github_pr_number"], r.get("skipped")) for r in results] == [
        (50, "pr_not_active"),
        (51, None),
    ]
    remaining = (await db_session.execute(select(PrReviewNudge))).scalars().all()
    assert [r.github_pr_number for r in remaining] == [51]


# --- a queued `opened` delivery processed after the merge -------------------------------


@pytest.fixture()
def qa_roster(monkeypatch: pytest.MonkeyPatch):
    """One eligible QA with a resolvable GitHub login, and a Plaky client that records."""
    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        # The bug-specialist shortcut would need a live board read; this test is about
        # what happens after a QA is chosen.
        qa_bug_specialist="",
        members=[
            TeamMember(
                id="qa-1",
                display=QA,
                github_login=QA,
                roles=["qa"],
                qa_tier=3.0,
                repo_globs=["*"],
            )
        ],
    )
    # The picker imports the loader into its own namespace, so both call sites need it.
    monkeypatch.setattr(ph, "load_team_assignments", lambda: cfg)
    monkeypatch.setattr("boardman.assignment.qa_picker.load_team_assignments", lambda: cfg)

    class FakePlaky:
        async def get_board_item_public(self, _board_id: str, item_id: str) -> dict[str, Any]:
            return {"ok": True, "item": {"id": item_id, "fld_qa": {"id": ""}}}

        async def resolve_space_for_board(self, _board_id: str) -> str:
            return "space-1"

    writes: list[str] = []

    async def fake_update(task_id, payload):
        writes.append(str(getattr(payload, "qa_plaky_id", "")))
        return {"ok": True}

    monkeypatch.setattr(ph, "PlakyClient", lambda *_a, **_kw: FakePlaky())
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    monkeypatch.setattr(
        ph, "_current_person_field_value", lambda *_a, **_kw: _async_none(), raising=False
    )
    return writes


async def _async_none() -> str:
    return ""


async def _run_assign_qa(session, *, pr_number: int = PR) -> dict[str, Any]:
    return await ph._assign_qa_for_pr(
        ph.PlakyClient(),
        task_id="task-50",
        board_id="board-1",
        repo_full=FULL,
        pr_number=pr_number,
        pr_author_login=DEV,
        task_url=f"https://github.com/{FULL}/pull/{pr_number}",
        session=session,
    )


def _no_github_write(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Fail loudly on either outward GitHub action: the @mention comment or the
    reviewer request."""
    posted: list[tuple] = []

    async def fail_comment(full_name, pr_number, body):
        posted.append(("comment", full_name, pr_number, body))
        raise AssertionError(f"must not @mention anyone on {full_name}#{pr_number}")

    async def fail_reviewers(full_name, pr_number, logins):
        posted.append(("reviewers", full_name, pr_number, tuple(logins)))
        raise AssertionError(f"must not request reviewers on {full_name}#{pr_number}")

    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", fail_comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", fail_reviewers)
    monkeypatch.setattr("boardman.github.pr_actions.has_qa_assignment_comment", fail_comment)
    return posted


@pytest.mark.asyncio
async def test_qa_assignment_does_not_mention_a_merged_pr(
    db_session, monkeypatch: pytest.MonkeyPatch, qa_roster
) -> None:
    """The queued-delivery race: `opened` enqueued, PR merged, `opened` then processed.

    The payload still says `state: open` -- that is the whole problem -- so this reads
    GitHub instead. The Plaky QA write still happens: it is the record of who was meant
    to review this work, and it moves no card status.
    """
    monkeypatch.setattr(ph, "pr_lifecycle_state", lambda *_a, **_kw: _terminal())
    posted = _no_github_write(monkeypatch)

    out = await _run_assign_qa(db_session)

    assert out["github_comment"] == {"ok": True, "skipped": "pr_not_active"}
    assert posted == []
    assert qa_roster == ["qa-1"], "the Plaky QA record must still be written"
    # ...and no escalation row was created for a PR there is no review to escalate.
    assert await nudges._get_row(db_session, REPO, PR) is None


async def _terminal() -> str:
    return pr_actions.PR_TERMINAL


@pytest.mark.asyncio
async def test_qa_assignment_still_mentions_an_open_pr(
    db_session, monkeypatch: pytest.MonkeyPatch, qa_roster
) -> None:
    """The guard must not disable the feature it protects."""
    monkeypatch.setattr(ph, "pr_lifecycle_state", lambda *_a, **_kw: _active())

    mentions: list[tuple] = []

    async def fake_comment(full_name, pr_number, body):
        mentions.append((full_name, pr_number, body))
        return {"ok": True}

    async def fake_reviewers(full_name, pr_number, logins):
        mentions.append(("reviewers", full_name, pr_number, tuple(logins)))
        return {"ok": True}

    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", fake_comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", fake_reviewers)

    out = await _run_assign_qa(db_session)

    assert "skipped" not in (out.get("github_comment") or {})
    assert f"@{QA}" in mentions[0][2]
    assert mentions[1][0] == "reviewers" and mentions[1][3] == (QA,)
    # An open PR still gets its escalation row, or the sweep could never nudge it.
    row = await nudges._get_row(db_session, REPO, PR)
    assert row is not None and row.primary_qa_login == QA


async def _active() -> str:
    return pr_actions.PR_ACTIVE


async def _unknown() -> str:
    return pr_actions.PR_STATE_UNKNOWN


@pytest.mark.asyncio
async def test_qa_assignment_proceeds_when_pr_state_is_unreadable(
    db_session, monkeypatch: pytest.MonkeyPatch, qa_roster
) -> None:
    """`unknown` is not `terminal`.

    GitHub being unreachable is not evidence that a PR finished, and blocking the
    assignment on it would leave open PRs with nobody asked to review them every time
    the API has a bad minute. Only a positive "merged or closed" stops the mention.
    """
    monkeypatch.setattr(ph, "pr_lifecycle_state", lambda *_a, **_kw: _unknown())

    mentions: list[str] = []

    async def fake_comment(_full, _pr, body):
        mentions.append(body)
        return {"ok": True}

    async def fake_reviewers(*_a, **_kw):
        return {"ok": True}

    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", fake_comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", fake_reviewers)

    out = await _run_assign_qa(db_session)

    assert "skipped" not in (out.get("github_comment") or {})
    assert f"@{QA}" in mentions[0]
