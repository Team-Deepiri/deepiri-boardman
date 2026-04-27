"""
End-to-end Plaky writes using synthetic GitHub webhook payloads (no GitHub REST for PR metadata).

Opt-in (writes real tasks, comments, and status updates on your board):
  PLAKY_PR_WORKFLOW_LIVE=1 poetry run pytest tests/test_plaky_pr_workflow_live.py -v --tb=short

Optional env:
  PLAKY_LIVE_QA_GITHUB_LOGIN=yourlogin — Plaky workspace user must expose this GitHub login (Plaky API user object).
  PLAKY_QA_ITEM_FIELD_KEY — optional; otherwise the QA assignee column is discovered from the board schema.
  PLAKY_LIVE_QA_APPROVED_OPTION_ID — optional override for the approve→verified status option UUID in the review test.

Requires PLAKY_API_KEY and default board/group (or first group). Status targets for reviews and IN QA are resolved
from the live board schema when env overrides are empty (see boardman.plaky.dynamic_qa_status).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, PullRequestTaskLink, SyncLog
from boardman.github.webhooks import (
    GitHubPullRequest,
    GitHubRepository,
    GitHubReview,
    IssueCommentEventPayload,
    IssueCommentIssuePayload,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
)
from boardman.plaky.client import PlakyClient, _headers
from boardman.plaky.dynamic_qa_status import discover_qa_assignee_field_key, resolve_plaky_status_patch
from boardman.repos_config import RepoRouting
from boardman.services.pr_handler import handle_pr_opened, handle_pr_review_comment
from boardman.services.pr_review_handler import handle_issue_comment_on_pr, handle_pull_request_review
from boardman.settings import settings
from boardman.main import create_app

pytestmark = [pytest.mark.plaky_live, pytest.mark.plaky_pr_workflow_live]


def _plaky_configured() -> bool:
    return bool((settings.plaky_api_key or "").strip())


skip_no_plaky = pytest.mark.skipif(
    not _plaky_configured(),
    reason="PLAKY_API_KEY missing — set in repo .env or export before pytest",
)

skip_live_writes = pytest.mark.skipif(
    os.environ.get("PLAKY_PR_WORKFLOW_LIVE") != "1",
    reason="Set PLAKY_PR_WORKFLOW_LIVE=1 to run live Plaky write tests.",
)


def _pick_board_id(boards: list) -> str:
    if settings.plaky_default_board_id.strip():
        return settings.plaky_default_board_id.strip()
    assert boards, "list_boards returned no boards"
    return str(boards[0]["id"])


async def _comments_http_contain(task_id: str, needle: str) -> bool:
    base = settings.plaky_api_base.rstrip("/")
    url = f"{base}/tasks/{task_id}/comments"
    key = (settings.plaky_api_key or "").strip()
    if not key:
        return False
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_headers(key), timeout=20.0)
    if r.status_code != 200:
        return False
    try:
        return needle in json.dumps(r.json())
    except ValueError:
        return False


async def _task_payload_contains_pr_opened(plaky: PlakyClient, board_id: str, task_id: str) -> bool:
    pub = await plaky.get_board_item_public(board_id, task_id)
    if pub.get("ok") and pub.get("item"):
        blob = json.dumps(pub["item"])
        if "PR Opened" in blob:
            return True
    gt = await plaky.get_task(task_id)
    if gt.get("ok") and gt.get("task"):
        blob = json.dumps(gt["task"])
        if "PR Opened" in blob:
            return True
    return await _comments_http_contain(task_id, "PR Opened")


async def _live_plaky_user_with_github(plaky: PlakyClient) -> tuple[str, str] | None:
    """Return (plaky_user_id, github_login) for a workspace user that has github_login set."""
    want = (os.environ.get("PLAKY_LIVE_QA_GITHUB_LOGIN") or "").strip().casefold()
    ur = await plaky.list_workspace_users()
    if not ur.get("ok"):
        return None
    first_with_gh: tuple[str, str] | None = None
    for u in ur.get("users") or []:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "").strip()
        gl = str(u.get("github_login") or "").strip()
        if not uid or not gl:
            continue
        if not first_with_gh:
            first_with_gh = (uid, gl)
        if want and gl.casefold() == want:
            return uid, gl
    return first_with_gh


async def _create_live_task(_plaky: PlakyClient, board_id: str, group_id: str, title: str) -> str:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": title,
                "description": "boardman live pytest - safe to delete",
                "repo": "deepiri-platform",
                "plaky_board_id": board_id,
                "plaky_group_id": group_id,
                "auto_assign_team": False,
            },
        )
    assert r.status_code == 200, r.text
    cr = r.json()
    assert cr.get("ok") is True, cr
    task = cr.get("task") or {}
    task_id = str(task.get("id") or task.get("taskId") or task.get("itemId") or cr.get("task_id") or "").strip()
    assert task_id, f"could not resolve task id from create_task: {task!r}"
    return task_id


async def _public_item_blob(plaky: PlakyClient, board_id: str, task_id: str) -> str:
    r = await plaky.get_board_item_public(board_id, task_id)
    assert r.get("ok"), r.get("message")
    return json.dumps(r.get("item") or {})


def _routing_factory(board_id: str):
    def _routing(*_a: Any, **_k: Any) -> RepoRouting:
        return RepoRouting(plaky_board_id=board_id)

    return _routing


async def _empty_github_participants(*_a: Any, **_k: Any) -> set[str]:
    return set()


@skip_no_plaky
@skip_live_writes
@pytest.mark.asyncio
async def test_live_handle_pr_opened_posts_plaky_comment_on_linked_issue_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = uuid.uuid4().hex[:10]
    issue_n = 8800000 + (int(marker[:6], 16) % 900000)
    repo_short = f"plaky-live-{marker}"

    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True, br.get("message")
    board_id = _pick_board_id(br["boards"])
    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    gid = (settings.plaky_default_group_id or "").strip()
    if not gid:
        assert groups, "need at least one group or PLAKY_DEFAULT_GROUP_ID"
        gid = str(groups[0]["id"])

    title = f"[boardman pytest PR workflow live {marker}] delete me"
    task_id = await _create_live_task(c, board_id, gid, title)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        session.add(
            IssueTaskMap(
                github_repo=repo_short,
                github_issue_number=issue_n,
                plaky_task_id=task_id,
                plaky_task_url=None,
            )
        )
        await session.commit()

    monkeypatch.setattr("boardman.repos_config.get_routing", _routing_factory(board_id))
    monkeypatch.setattr(settings, "plaky_skip_needs_qa_for_draft", True)
    monkeypatch.setattr(settings, "github_org", "deepiri-org")

    pr_number = 9000 + (int(marker[:4], 16) % 999)
    pr_url = f"https://github.com/deepiri-org/{repo_short}/pull/{pr_number}"
    payload = PullRequestEventPayload(
        action="opened",
        pull_request=GitHubPullRequest(
            number=pr_number,
            title=f"live test {marker}",
            html_url=pr_url,
            state="open",
            merged=False,
            draft=True,
            body=f"Fixes #{issue_n}\n\nAutomated Plaky live test.",
        ),
        repository=GitHubRepository(
            full_name=f"deepiri-org/{repo_short}",
            name=repo_short,
        ),
    )

    async with factory() as session:
        out = await handle_pr_opened(payload, session)

    assert out.get("ok") is True, out
    linked = out.get("linked") or []
    assert any(x.get("task_id") == task_id for x in linked), out

    async with factory() as session:
        logged = await session.scalar(
            select(SyncLog.id).where(
                SyncLog.action == "pr_linked",
                SyncLog.plaky_task_id == task_id,
            )
        )
    assert logged is not None, "expected pr_linked SyncLog after handle_pr_opened"

    await engine.dispose()


@skip_no_plaky
@skip_live_writes
@pytest.mark.asyncio
async def test_live_issue_comment_by_plaky_assigned_qa_sets_in_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "plaky_pr_in_qa_status", "")
    monkeypatch.setattr(settings, "plaky_status_in_qa", "")

    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True, br.get("message")
    board_id = _pick_board_id(br["boards"])

    pair = await _live_plaky_user_with_github(c)
    if not pair:
        pytest.skip(
            "No Plaky workspace user with github_login; link GitHub on a Plaky user or set PLAKY_LIVE_QA_GITHUB_LOGIN"
        )
    member_id, member_login = pair

    qa_field = (settings.plaky_qa_item_field_key or "").strip()
    if not qa_field:
        qa_field = (await discover_qa_assignee_field_key(board_id)) or ""
    if not qa_field:
        pytest.skip("Set PLAKY_QA_ITEM_FIELD_KEY or use a board with a discoverable QA person column")

    rp = await resolve_plaky_status_patch(board_id, intent="workflow_in_qa")
    if not rp:
        pytest.skip("Board schema has no status option matching In QA hints")
    _, in_qa_option_id = rp

    marker = uuid.uuid4().hex[:8]
    repo_short = f"plaky-live-ic-{marker}"
    pr_number = 9100 + (int(marker[:4], 16) % 899)

    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    gid = (settings.plaky_default_group_id or "").strip() or str(groups[0]["id"])

    title = f"[boardman live issue_comment {marker}] delete me"
    task_id = await _create_live_task(c, board_id, gid, title)

    patch = await c.patch_item_field_values(board_id, task_id, {qa_field: member_id})
    assert patch.get("ok") is True, patch.get("message")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo=repo_short,
                github_pr_number=pr_number,
                plaky_task_id=task_id,
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    monkeypatch.setattr("boardman.repos_config.get_routing", _routing_factory(board_id))
    monkeypatch.setattr(
        "boardman.services.pr_review_handler.fetch_pr_assignees_and_reviewers_logins",
        _empty_github_participants,
    )

    payload = IssueCommentEventPayload(
        action="created",
        issue=IssueCommentIssuePayload(number=pr_number, pull_request={"url": "http://api.github.com"}),
        comment={"user": {"login": member_login}, "body": "live test: QA on thread"},
        repository=GitHubRepository(full_name=f"deepiri-org/{repo_short}", name=repo_short),
    )

    async with factory() as session:
        out = await handle_issue_comment_on_pr(payload, session)

    assert out.get("ok") is True, out
    assert out.get("skipped") is not True, out
    updated = out.get("updated") or []
    assert updated and all(u.get("plaky", {}).get("ok") for u in updated), out

    blob = (await _public_item_blob(c, board_id, task_id)).casefold()
    assert in_qa_option_id.casefold() in blob, f"expected status option {in_qa_option_id!r} on task {task_id}; snippet={blob[:800]}"

    await engine.dispose()


@skip_no_plaky
@skip_live_writes
@pytest.mark.asyncio
async def test_live_pull_request_review_submitted_approved_on_plaky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "plaky_pr_qa_approved_status", "")
    monkeypatch.setattr(settings, "plaky_status_qa_approved", "")

    marker = uuid.uuid4().hex[:8]
    repo_short = f"plaky-live-rev-{marker}"
    pr_number = 9200 + (int(marker[:4], 16) % 899)

    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True, br.get("message")
    board_id = _pick_board_id(br["boards"])
    forced = (os.environ.get("PLAKY_LIVE_QA_APPROVED_OPTION_ID") or "").strip()
    if forced:
        approved_option_id = forced
        monkeypatch.setattr(settings, "plaky_pr_qa_approved_status", approved_option_id)
    else:
        rp = await resolve_plaky_status_patch(board_id, intent="github_pr_review_approved")
        if not rp:
            pytest.skip(
                "No status option on this board matches GitHub approve → QA verified hints; "
                "set PLAKY_LIVE_QA_APPROVED_OPTION_ID or PLAKY_PR_QA_APPROVED_STATUS to a Plaky option UUID."
            )
        _, approved_option_id = rp

    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    gid = (settings.plaky_default_group_id or "").strip() or str(groups[0]["id"])

    title = f"[boardman live review approved {marker}] delete me"
    task_id = await _create_live_task(c, board_id, gid, title)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo=repo_short,
                github_pr_number=pr_number,
                plaky_task_id=task_id,
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    monkeypatch.setattr("boardman.repos_config.get_routing", _routing_factory(board_id))

    review = PullRequestReviewEventPayload(
        action="submitted",
        review=GitHubReview(user={"login": "any-external-reviewer"}, state="approved"),
        pull_request=GitHubPullRequest(
            number=pr_number,
            title="live",
            html_url=f"https://github.com/deepiri-org/{repo_short}/pull/{pr_number}",
            state="open",
            merged=False,
            body="",
        ),
        repository=GitHubRepository(full_name=f"deepiri-org/{repo_short}", name=repo_short),
    )

    async with factory() as session:
        out = await handle_pull_request_review(review, session)

    assert out.get("ok") is True, out
    updated = out.get("updated") or []
    assert updated and all(u.get("plaky", {}).get("ok") for u in updated), out

    blob = (await _public_item_blob(c, board_id, task_id)).casefold()
    assert approved_option_id.casefold() in blob, (
        f"expected status option {approved_option_id!r} on task {task_id}; snippet={blob[:800]}"
    )

    await engine.dispose()


@skip_no_plaky
@skip_live_writes
@pytest.mark.asyncio
async def test_live_pr_review_comment_by_assigned_qa_sets_in_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "plaky_pr_in_qa_status", "")
    monkeypatch.setattr(settings, "plaky_status_in_qa", "")

    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True, br.get("message")
    board_id = _pick_board_id(br["boards"])

    pair = await _live_plaky_user_with_github(c)
    if not pair:
        pytest.skip(
            "No Plaky workspace user with github_login; link GitHub on a Plaky user or set PLAKY_LIVE_QA_GITHUB_LOGIN"
        )
    member_id, member_login = pair

    qa_field = (settings.plaky_qa_item_field_key or "").strip()
    if not qa_field:
        qa_field = (await discover_qa_assignee_field_key(board_id)) or ""
    if not qa_field:
        pytest.skip("Set PLAKY_QA_ITEM_FIELD_KEY or use a board with a discoverable QA person column")

    rp = await resolve_plaky_status_patch(board_id, intent="workflow_in_qa")
    if not rp:
        pytest.skip("Board schema has no status option matching In QA hints")
    _, in_qa_option_id = rp

    marker = uuid.uuid4().hex[:8]
    repo_short = f"plaky-live-rc-{marker}"
    pr_number = 9300 + (int(marker[:4], 16) % 899)

    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    gid = (settings.plaky_default_group_id or "").strip() or str(groups[0]["id"])

    title = f"[boardman live review_comment {marker}] delete me"
    task_id = await _create_live_task(c, board_id, gid, title)

    patch = await c.patch_item_field_values(board_id, task_id, {qa_field: member_id})
    assert patch.get("ok") is True, patch.get("message")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo=repo_short,
                github_pr_number=pr_number,
                plaky_task_id=task_id,
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    monkeypatch.setattr("boardman.repos_config.get_routing", _routing_factory(board_id))

    pr = GitHubPullRequest(
        number=pr_number,
        title="live",
        html_url=f"https://github.com/deepiri-org/{repo_short}/pull/{pr_number}",
        state="open",
        merged=False,
        body="no linked issues",
    )
    payload = PullRequestReviewCommentEventPayload(
        action="created",
        comment={"user": {"login": member_login}, "body": "review thread QA ping"},
        pull_request=pr,
        repository=GitHubRepository(full_name=f"deepiri-org/{repo_short}", name=repo_short),
    )

    async with factory() as session:
        out = await handle_pr_review_comment(payload, session)

    assert out.get("ok") is True, out
    upd = out.get("updated") or []
    assert upd, out

    blob = (await _public_item_blob(c, board_id, task_id)).casefold()
    assert in_qa_option_id.casefold() in blob, f"expected {in_qa_option_id!r} on task {task_id}; snippet={blob[:800]}"
    assert "pr comment" in blob, f"expected Plaky note comment on task {task_id}; snippet={blob[:800]}"

    await engine.dispose()
