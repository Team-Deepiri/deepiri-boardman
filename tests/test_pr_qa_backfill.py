"""A PR linked by any path other than a fresh handle_pr_opened must still get a QA.

QA assignment is documented as a one-time event at PR-open (`_assign_qa_for_pr`'s own
docstring). Reconciliation can link an already-open PR to an existing task via fuzzy
matching, or a link row can predate a bug fix -- either way, that PR never goes through
handle_pr_opened, so without a backfill it would sit with no QA forever: every later
reconcile pass only re-syncs metadata via handle_pr_edited, never revisits QA.

Found live: a real PR (diri-cyrex#179) was correctly linked to its Plaky task but had
no QA assigned and no GitHub comment, while a PR that went through handle_pr_opened
fresh (deepiri-boardman#98) had both.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, PullRequestTaskLink
from boardman.github.webhooks import PullRequestEventPayload
from boardman.services import pr_handler as ph

REPO = "deepiri-boardman"
FULL = "Team-Deepiri/deepiri-boardman"


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _edited_payload(number: int) -> PullRequestEventPayload:
    return PullRequestEventPayload(
        action="edited",
        pull_request={
            "number": number,
            "title": "Fix the thing",
            "body": "no issue reference",
            "html_url": f"https://github.com/{FULL}/pull/{number}",
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "ali-ferris"},
            "head": {"ref": "feat/x"},
        },
        repository={"full_name": FULL, "name": REPO},
    )


class _Routing:
    plaky_board_id = "269031"
    plaky_group_id = "g1"


@pytest.mark.asyncio
async def test_handle_pr_edited_backfills_qa_for_a_linked_task_with_none(
    db_session, monkeypatch
) -> None:
    async def fake_routing(*_a, **_k):
        return _Routing()

    async def fake_update(_task_id, _inp):
        return {"ok": True}

    async def task_ids(_session, *, github_repo, github_pr_number):
        return ["task-no-qa"]

    calls: list[dict] = []

    async def fake_assign_qa(_plaky, **kwargs):
        calls.append(kwargs)
        return {"plaky_qa": {"id": "999", "ok": True}}

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", task_ids)
    monkeypatch.setattr(ph, "_assign_qa_for_pr", fake_assign_qa)

    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=179,
            plaky_task_id="task-no-qa",
            github_issue_number=0,
            link_source="auto_link",
        )
    )
    await db_session.commit()

    result = await ph.handle_pr_edited(_edited_payload(179), db_session)

    assert result["event"] == "pr_metadata_synced"
    assert len(calls) == 1
    assert calls[0]["task_id"] == "task-no-qa"
    assert calls[0]["pr_number"] == 179


@pytest.mark.asyncio
async def test_handle_pr_edited_skips_qa_backfill_for_a_draft_pr(db_session, monkeypatch) -> None:
    async def fake_routing(*_a, **_k):
        return _Routing()

    async def fake_update(_task_id, _inp):
        return {"ok": True}

    async def task_ids(_session, *, github_repo, github_pr_number):
        return ["task-draft"]

    calls: list[dict] = []

    async def fake_assign_qa(_plaky, **kwargs):
        calls.append(kwargs)
        return {"plaky_qa": {"id": "999", "ok": True}}

    monkeypatch.setattr("boardman.repos_config.get_routing_async", fake_routing)
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    monkeypatch.setattr(ph, "distinct_task_ids_for_pr", task_ids)
    monkeypatch.setattr(ph, "_assign_qa_for_pr", fake_assign_qa)

    db_session.add(
        PullRequestTaskLink(
            github_repo=REPO,
            github_pr_number=180,
            plaky_task_id="task-draft",
            github_issue_number=0,
            link_source="auto_link",
        )
    )
    await db_session.commit()

    payload = _edited_payload(180)
    payload.pull_request.draft = True

    await ph.handle_pr_edited(payload, db_session)

    assert calls == [], "a draft PR must not get a QA assigned/notified yet"
