"""Beyond-spec lifecycle handlers: issue close/reopen, PR edit/draft,
dismissal, triage visibility, branch-ref guards."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, SyncLog
from boardman.github.webhooks import (
    IssueCommentEventPayload,
    IssueEventPayload,
    PullRequestEventPayload,
    PullRequestReviewEventPayload,
)
from boardman.services import issue_handler as ih
from boardman.services import pr_handler as ph
from boardman.services import pr_review_handler as prh
from boardman.services.pr_task_linking import branch_issue_numbers, referenced_issue_numbers

# --- branch-number guard ---------------------------------------------------------


def test_branch_numbers_keyword_and_leading_only() -> None:
    assert branch_issue_numbers("issue-42") == {42}
    assert branch_issue_numbers("fix/42-sync") == {42}
    assert branch_issue_numbers("feature/42-bugfix") == {42}
    assert branch_issue_numbers("42-add-tests") == {42}
    assert branch_issue_numbers("gh-123/cleanup") == {123}
    assert branch_issue_numbers("refs/heads/bug_7") == {7}
    # The false positives that used to auto-link PRs to unrelated tasks:
    assert branch_issue_numbers("upgrade-node-20") == set()
    assert branch_issue_numbers("migrate-py-311") == set()
    assert branch_issue_numbers("feature/add-oauth2") == set()


def test_referenced_issue_numbers_still_covers_urls_and_hashes() -> None:
    out = referenced_issue_numbers(
        repo_full="o/r",
        pr_title="Fix parser",
        pr_body="see #12 and https://github.com/o/r/issues/34",
        head_ref="upgrade-node-20",
    )
    assert out == {12, 34}


# --- shared fakes ----------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _issue_payload(action: str, number: int = 5) -> IssueEventPayload:
    return IssueEventPayload(
        action=action,
        issue={
            "number": number,
            "title": "T",
            "html_url": f"https://github.com/o/r/issues/{number}",
        },
        repository={"full_name": "o/r", "name": "r"},
    )


class _FakePlaky:
    comments: list[tuple[str, str]] = []

    def __init__(self) -> None:
        pass

    async def add_comment(self, task_id: str, body: str, **kw: Any) -> dict:
        _FakePlaky.comments.append((str(task_id), body))
        return {"ok": True}


@pytest.fixture()
def fake_plaky(monkeypatch: pytest.MonkeyPatch) -> type[_FakePlaky]:
    _FakePlaky.comments = []
    monkeypatch.setattr(ih, "PlakyClient", _FakePlaky)
    monkeypatch.setattr(prh, "PlakyClient", _FakePlaky)
    return _FakePlaky


# --- issues.closed / reopened ----------------------------------------------------


@pytest.mark.asyncio
async def test_issue_closed_completes_mapped_task(db_session, monkeypatch, fake_plaky) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=5, plaky_task_id="task-1"))
    await db_session.flush()

    updates: list[tuple[str, Any]] = []

    async def fake_update(task_id: str, inp: Any) -> dict:
        updates.append((task_id, inp))
        return {"ok": True}

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)
    monkeypatch.setattr(ih, "get_routing_async", fake_routing)

    res = await ih.handle_issue_closed(_issue_payload("closed"), db_session)
    assert res["ok"] is True and res.get("status") == "completed"
    assert updates and updates[0][0] == "task-1"
    assert updates[0][1].status == "completed"
    assert any("Issue closed" in c[1] for c in fake_plaky.comments)
    q = select(SyncLog).where(SyncLog.action == "issue_closed")
    logs = (await db_session.execute(q)).scalars().all()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_issue_closed_without_mapping_skips(db_session, monkeypatch) -> None:
    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    res = await ih.handle_issue_closed(_issue_payload("closed", number=999), db_session)
    assert res.get("skipped") is True


@pytest.mark.asyncio
async def test_issue_reopened_skips_without_resolvable_status(
    db_session, monkeypatch, fake_plaky
) -> None:
    """No board id and no literal fallback → reopen is a safe no-op."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=5, plaky_task_id="task-1"))
    await db_session.flush()

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    res = await ih.handle_issue_reopened(_issue_payload("reopened"), db_session)
    assert res.get("skipped") is True


# --- pr.edited -------------------------------------------------------------------


def _pr_payload(
    action: str, number: int = 9, state: str = "open", merged: bool = False
) -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action=action,
        pull_request={
            "number": number,
            "title": "x",
            "html_url": f"https://github.com/o/r/pull/{number}",
            "state": state,
            "merged": merged,
        },
        repository={"full_name": "o/r", "name": "r"},
    )


@pytest.mark.asyncio
async def test_pr_edited_reruns_pipeline_only_when_unlinked(db_session, monkeypatch) -> None:
    opened_calls: list[int] = []

    async def fake_opened(payload: Any, session: Any) -> dict:
        opened_calls.append(payload.pull_request.number)
        return {"ok": True, "reran": True}

    async def no_tasks(session: Any, *, github_repo: str, github_pr_number: int) -> list[str]:
        return []

    monkeypatch.setattr(ph, "handle_pr_opened", fake_opened)
    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", no_tasks)
    res = await ph.handle_pr_edited(_pr_payload("edited"), db_session)
    assert res.get("reran") is True and opened_calls == [9]

    async def linked(session: Any, *, github_repo: str, github_pr_number: int) -> list[str]:
        return ["t1"]

    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", linked)
    res2 = await ph.handle_pr_edited(_pr_payload("edited"), db_session)
    assert res2.get("skipped") is True and opened_calls == [9]


@pytest.mark.asyncio
async def test_pr_edited_ignores_closed_pr(db_session) -> None:
    res = await ph.handle_pr_edited(_pr_payload("edited", state="closed"), db_session)
    assert res.get("skipped") is True


# --- pr.converted_to_draft -------------------------------------------------------


@pytest.mark.asyncio
async def test_converted_to_draft_reverts_needs_qa(db_session, monkeypatch) -> None:
    async def linked(session: Any, *, github_repo: str, github_pr_number: int) -> list[str]:
        return ["t1"]

    class R:
        plaky_board_id = "b1"

    async def fake_routing(*a: Any, **kw: Any) -> R:
        return R()

    async def fake_resolve(bid: str, *, intent: str):
        return {
            "workflow_needs_qa": ("status-6", "nq-id"),
            "workflow_in_progress": ("status-6", "ip-id"),
        }.get(intent)

    async def fake_current(plaky: Any, bid: str, tid: str, key: str) -> str:
        return "nq-id"

    updates: list[tuple[str, str]] = []

    async def fake_update(tid: str, val: str, bid: str, *, status_field_key=None) -> dict:
        updates.append((tid, val))
        return {"ok": True}

    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", linked)
    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr(ph, "_current_status_value", fake_current)
    monkeypatch.setattr(ph, "_update_plaky_task_status", fake_update)

    res = await ph.handle_pr_converted_to_draft(_pr_payload("converted_to_draft"), db_session)
    assert res["event"] == "converted_to_draft"
    assert updates == [("t1", "ip-id")]


# --- review dismissed ------------------------------------------------------------


def _review_payload(action: str, state: str = "approved") -> PullRequestReviewEventPayload:
    return PullRequestReviewEventPayload(
        action=action,
        review={"state": state, "user": {"login": "qa-person"}},
        pull_request={
            "number": 3,
            "title": "x",
            "html_url": "https://github.com/o/r/pull/3",
            "state": "open",
        },
        repository={"full_name": "o/r", "name": "r"},
    )


@pytest.mark.asyncio
async def test_review_dismissed_reverts_approved_to_in_qa(db_session, monkeypatch) -> None:
    async def linked(session: Any, *, github_repo: str, github_pr_number: int) -> list[str]:
        return ["t1", "t2"]

    class R:
        plaky_board_id = "b1"

    async def fake_routing(*a: Any, **kw: Any) -> R:
        return R()

    async def fake_resolve(bid: str, *, intent: str):
        return {
            "github_pr_review_approved": ("status-6", "ap-id"),
            "workflow_in_qa": ("status-6", "iq-id"),
        }.get(intent)

    async def fake_current(plaky: Any, bid: str, tid: str, key: str) -> str:
        return "ap-id" if tid == "t1" else "other"

    updates: list[tuple[str, str]] = []

    async def fake_update(tid: str, val: str, bid: str, *, status_field_key=None) -> dict:
        updates.append((tid, val))
        return {"ok": True}

    monkeypatch.setattr(prh, "distinct_task_ids_for_pr", linked)
    monkeypatch.setattr(prh, "get_routing_async", fake_routing)
    monkeypatch.setattr(prh, "resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.services.pr_handler._current_status_value", fake_current)
    monkeypatch.setattr(prh, "_update_plaky_task_status", fake_update)

    res = await prh.handle_pull_request_review(_review_payload("dismissed"), db_session)
    assert res["event"] == "review_dismissed"
    # Only the task actually sitting in QA-approved is reverted.
    assert updates == [("t1", "iq-id")]


# --- plain issue comment sync ----------------------------------------------------


@pytest.mark.asyncio
async def test_plain_issue_comment_lands_on_task(db_session, monkeypatch, fake_plaky) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=5, plaky_task_id="task-9"))
    await db_session.flush()

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(prh, "get_routing_async", fake_routing)
    payload = IssueCommentEventPayload(
        action="created",
        issue={"number": 5, "title": "T", "html_url": "https://github.com/o/r/issues/5"},
        comment={
            "user": {"login": "Blasted-ctrl"},
            "body": "QA note: repro steps attached",
            "html_url": "https://github.com/o/r/issues/5#issuecomment-1",
        },
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await prh.handle_issue_comment_on_pr(payload, db_session)
    assert res.get("event") == "issue_comment_synced"
    assert fake_plaky.comments and fake_plaky.comments[0][0] == "task-9"
    assert "Blasted-ctrl" in fake_plaky.comments[0][1]
    assert "repro steps" in fake_plaky.comments[0][1]


@pytest.mark.asyncio
async def test_plain_issue_comment_synced_only_once(db_session, monkeypatch, fake_plaky) -> None:
    """Restarting the poller replays recent events from the catch-up window. Status writes
    are idempotent but comments are additive, so the same GitHub comment must not be
    mirrored twice — dedupe on the comment url via SyncLog, not an in-memory set."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=5, plaky_task_id="task-9"))
    await db_session.flush()

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(prh, "get_routing_async", fake_routing)

    def payload_for(url: str) -> IssueCommentEventPayload:
        return IssueCommentEventPayload(
            action="created",
            issue={"number": 5, "title": "T", "html_url": "https://github.com/o/r/issues/5"},
            comment={
                "user": {"login": "Blasted-ctrl"},
                "body": "QA note: repro steps attached",
                "html_url": url,
            },
            repository={"full_name": "o/r", "name": "r"},
        )

    base = "https://github.com/o/r/issues/5#issuecomment-123"
    first = await prh.handle_issue_comment_on_pr(payload_for(base), db_session)
    assert first.get("event") == "issue_comment_synced"

    replay = await prh.handle_issue_comment_on_pr(payload_for(base), db_session)
    assert replay.get("skipped") is True
    assert len(fake_plaky.comments) == 1, "replayed comment was mirrored twice"

    # A longer id must not be swallowed by prefix-matching the shorter one.
    other = await prh.handle_issue_comment_on_pr(payload_for(base + "4"), db_session)
    assert other.get("event") == "issue_comment_synced"
    assert len(fake_plaky.comments) == 2


@pytest.mark.asyncio
async def test_boardman_does_not_react_to_its_own_comment(db_session, fake_plaky) -> None:
    """Boardman comments as the PAT owner — a real teammate, not a '[bot]' login. Its
    QA-assignment comment came back through the event feed as "a support-team member
    commented on the PR" and pushed the task to In QA before QA had opened it."""
    from boardman.github.pr_actions import with_marker

    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=5, plaky_task_id="task-9"))
    await db_session.flush()

    payload = IssueCommentEventPayload(
        action="created",
        issue={
            "number": 5,
            "title": "T",
            "html_url": "https://github.com/o/r/issues/5",
            "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/5"},
        },
        comment={
            "user": {"login": "Blasted-ctrl"},
            "body": with_marker("@Blasted-ctrl you've been assigned as **QA reviewer**"),
            "html_url": "https://github.com/o/r/issues/5#issuecomment-9",
        },
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await prh.handle_issue_comment_on_pr(payload, db_session)
    assert res.get("skipped") is True
    assert "own comment" in res.get("message", "")
    assert not fake_plaky.comments


@pytest.mark.asyncio
async def test_plain_issue_comment_from_bot_ignored(db_session, fake_plaky) -> None:
    payload = IssueCommentEventPayload(
        action="created",
        issue={"number": 5, "title": "T", "html_url": "https://github.com/o/r/issues/5"},
        comment={"user": {"login": "dependabot[bot]"}, "body": "bump"},
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await prh.handle_issue_comment_on_pr(payload, db_session)
    assert res.get("skipped") is True and not fake_plaky.comments


# --- triage idempotency ----------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_is_idempotent_per_pr(db_session, monkeypatch) -> None:
    class Amb:
        enabled = True
        triage_board_id = "b1"
        triage_group_id = "g1"
        assign_qa = False
        title_template = "Triage: PR #{number} — {repo}"

    class Cfg:
        ambiguous_pr = Amb()
        plaky_field_qa = ""

    monkeypatch.setattr(ph, "load_team_assignments", lambda: Cfg())
    db_session.add(
        SyncLog(action="pr_ambiguous_triage", github_repo="r", github_ref="9", detail="{}")
    )
    await db_session.flush()

    res = await ph._maybe_triage_ambiguous_pr(_pr_payload("opened"), db_session)
    assert res is not None and res.get("skipped") is True
    assert "already created" in res.get("message", "")


# --- label changes after creation ------------------------------------------------


@pytest.mark.asyncio
async def test_label_added_after_creation_resyncs_type(db_session, monkeypatch) -> None:
    """Issue #80: filed bare (task typed Story via the Feature default), `bug` labeled 75s
    later. Labels are the team's explicit categorization act — the change must reach the
    task instead of freezing the creation-time race."""
    import boardman.services.issue_handler as ih
    from boardman.services.issue_handler import handle_issue_labels_changed

    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=80, plaky_task_id="task-80"))
    await db_session.flush()

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(ih, "get_routing_async", fake_routing)

    calls: list[tuple[str, str]] = []

    async def fake_update(task_id, inp):
        calls.append((task_id, inp.task_type))
        return {"ok": True}

    import boardman.services.task_mutations as tm

    monkeypatch.setattr(tm, "update_task_internal", fake_update)

    payload = IssueEventPayload(
        action="labeled",
        issue={
            "number": 80,
            "title": "showcase",
            "html_url": "https://github.com/o/r/issues/80",
            "labels": [{"name": "bug"}, {"name": "NEEDS HELP"}],
        },
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await handle_issue_labels_changed(payload, db_session)
    assert res.get("event") == "issue_labels_synced"
    assert calls == [("task-80", "Bug")]


@pytest.mark.asyncio
async def test_label_change_on_unmapped_issue_is_skipped(db_session) -> None:
    from boardman.services.issue_handler import handle_issue_labels_changed

    payload = IssueEventPayload(
        action="labeled",
        issue={"number": 999, "title": "t", "html_url": "u", "labels": [{"name": "bug"}]},
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await handle_issue_labels_changed(payload, db_session)
    assert res.get("skipped") is True
