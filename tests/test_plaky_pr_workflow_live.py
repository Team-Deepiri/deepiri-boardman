"""
End-to-end Plaky writes using synthetic GitHub webhook payloads (no GitHub API).

Requires a valid key and explicit opt-in:
  PLAKY_PR_WORKFLOW_LIVE=1 poetry run pytest tests/test_plaky_pr_workflow_live.py -v --tb=short

Creates a real Plaky task (safe to delete from the board UI), posts a PR-opened-style
comment via handle_pr_opened, then checks the item payload for that text.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap
from boardman.github.webhooks import GitHubPullRequest, GitHubRepository, PullRequestEventPayload
from boardman.plaky.client import PlakyClient, _headers
from boardman.repos_config import RepoRouting
from boardman.services.pr_handler import handle_pr_opened
from boardman.settings import settings

pytestmark = [pytest.mark.plaky_live, pytest.mark.plaky_pr_workflow_live]


def _plaky_configured() -> bool:
    return bool((settings.plaky_api_key or "").strip())


skip_no_plaky = pytest.mark.skipif(
    not _plaky_configured(),
    reason="PLAKY_API_KEY missing — set in repo .env or export before pytest",
)


def _pick_board_id(boards: list) -> str:
    if settings.plaky_default_board_id.strip():
        return settings.plaky_default_board_id.strip()
    assert boards, "list_boards returned no boards"
    return str(boards[0]["id"])


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
    base = settings.plaky_api_base.rstrip("/")
    url = f"{base}/tasks/{task_id}/comments"
    key = (settings.plaky_api_key or "").strip()
    if not key:
        return False
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=_headers(key), timeout=20.0)
    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError:
            return False
        blob = json.dumps(data)
        return "PR Opened" in blob
    return False


@skip_no_plaky
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PLAKY_PR_WORKFLOW_LIVE") != "1",
    reason="Set PLAKY_PR_WORKFLOW_LIVE=1 to run this test (creates a Plaky task and comment).",
)
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
    cr = await c.create_task(
        title=title,
        description="Automated live test; safe to delete.",
        priority="low",
        board_id=board_id,
        group_id=gid,
    )
    assert cr.get("ok") is True, cr.get("message")
    task = cr.get("task") or {}
    task_id = str(task.get("id") or task.get("taskId") or task.get("itemId") or "").strip()
    assert task_id, f"could not resolve task id from create_task payload: {task!r}"

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
                plaky_task_url=cr.get("task_url"),
            )
        )
        await session.commit()

    def _routing(*_a: Any, **_k: Any) -> RepoRouting:
        return RepoRouting(plaky_board_id=board_id)

    monkeypatch.setattr("boardman.services.pr_handler.get_routing", _routing)
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

    assert await _task_payload_contains_pr_opened(c, board_id, task_id), (
        "Expected Plaky item or task payload to contain 'PR Opened' after handle_pr_opened; "
        f"issue=#{issue_n} task_id={task_id!r}"
    )

    await engine.dispose()
