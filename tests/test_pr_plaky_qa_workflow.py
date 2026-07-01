"""
Plaky QA workflow: PR review states, merge completion, review comments (assigned QA),
issue comments (assignee/reviewer), fuzzy-linked PR task ids.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember
from boardman.database.models import Base, PullRequestTaskLink
from boardman.github.webhooks import (
    IssueCommentEventPayload,
    IssueCommentIssuePayload,
    GitHubPullRequest,
    GitHubRepository,
    PullRequestEventPayload,
    PullRequestReviewCommentEventPayload,
    PullRequestReviewEventPayload,
    GitHubReview,
)
from boardman.services.pr_handler import (
    handle_pr_merged,
    handle_pr_review_comment,
    handle_pr_review_requested,
)
from boardman.services.pr_review_handler import handle_issue_comment_on_pr, handle_pull_request_review
from boardman.repos_config import RepoRouting
from boardman.settings import settings


async def _schema_off(*_a, **_k):
    """Force legacy /tasks status patch path (no board schema) in tests."""
    return {"ok": False}


def _patch_task_mutations_plaky(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", lambda: fake)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_off)


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class RecordingPlaky:
    """Captures status updates and comments."""

    def __init__(self, *, qa_field: str = "fld_qa", qa_plaky_id: str = "qa-plaky-1"):
        self.qa_field = qa_field
        self.qa_plaky_id = qa_plaky_id
        self.status_calls: List[tuple[str, str]] = []
        self.comments: List[tuple[str, str]] = []
        self.board_id = "board-1"

    async def get_board_item_public(self, board_id: str, item_id: str) -> Dict[str, Any]:
        return {
            "ok": True,
            "item": {
                "id": item_id,
                self.qa_field: {"id": self.qa_plaky_id, "name": "QA Person"},
            },
        }

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        return {"ok": True, "task": {"boardId": self.board_id, "id": task_id}}

    async def update_task_fields(self, task_id: str, **kwargs: Any) -> Dict[str, Any]:
        st = kwargs.get("status")
        if st is not None:
            self.status_calls.append((task_id, str(st)))
        return {"ok": True}

    async def patch_item_field_values(self, board_id: str, item_id: str, values: dict) -> Dict[str, Any]:
        st = next(iter(values.values()), "")
        self.status_calls.append((item_id, str(st)))
        return {"ok": True}

    async def add_comment(self, task_id: str, body: str, **kwargs: Any) -> Dict[str, Any]:
        self.comments.append((task_id, body))
        return {"ok": True}


@pytest.fixture
def qa_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "plaky_pr_needs_qa_status", "needs_qa")
    monkeypatch.setattr(settings, "plaky_pr_in_qa_status", "in_qa")
    monkeypatch.setattr(settings, "plaky_pr_qa_approved_status", "qa_approved")
    monkeypatch.setattr(settings, "plaky_pr_qa_rejected_status", "qa_rejected")
    monkeypatch.setattr(settings, "plaky_status_needs_qa", "needs_qa")
    monkeypatch.setattr(settings, "plaky_status_in_qa", "in_qa")
    monkeypatch.setattr(settings, "plaky_status_qa_approved", "qa_approved")
    monkeypatch.setattr(settings, "plaky_status_qa_rejected", "qa_rejected")
    monkeypatch.setattr(settings, "plaky_status_completed", "completed")
    monkeypatch.setattr(settings, "plaky_pr_merge_status", "completed")

    async def _fixed_routing(*a, **k):
        return RepoRouting(plaky_board_id="board-1")

    monkeypatch.setattr("boardman.repos_config.get_routing_async", _fixed_routing)
    monkeypatch.setattr("boardman.services.pr_review_handler.get_routing_async", _fixed_routing)
    monkeypatch.setattr("boardman.services.pr_task_linking.get_routing_async", _fixed_routing)
    monkeypatch.setattr(settings, "plaky_complete_when_all_prs_merged", False)
    monkeypatch.setattr(settings, "github_org", "deepiri-org")

    # QA-field resolution is now schema-first; provide a board schema so discovery returns
    # the test's QA field key ("fld_qa") instead of hitting the live Plaky API.
    async def _fake_normalized(_bid):
        return {"fields": [{"key": "fld_qa", "name": "QA Engineer Assigned", "type": "person"}]}

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status._load_normalized", _fake_normalized)


@pytest.mark.asyncio
async def test_pull_request_review_approved_any_reviewer_to_qa_approved(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky()
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=55,
                plaky_task_id="task-99",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    review = PullRequestReviewEventPayload(
        action="submitted",
        review=GitHubReview(user={"login": "external-reviewer"}, state="approved"),
        pull_request=GitHubPullRequest(
            number=55,
            title="t",
            html_url="http://pr",
            state="open",
            merged=False,
            body="",
        ),
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pull_request_review(review, session)

    assert out.get("ok") is True
    assert fake.status_calls == [("task-99", "qa_approved")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_pull_request_review_changes_requested_only_by_assigned_qa_moves_rejected(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky(qa_field="fld_qa", qa_plaky_id="qa-plaky-1")
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        members=[TeamMember(id="qa-plaky-1", github_login="qa-engineer", display="QE", qa_tier=3)],
    )
    monkeypatch.setattr("boardman.services.pr_review_handler.load_team_assignments", lambda: cfg)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=56,
                plaky_task_id="task-100",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    review = PullRequestReviewEventPayload(
        action="submitted",
        review=GitHubReview(user={"login": "qa-engineer"}, state="changes_requested"),
        pull_request=GitHubPullRequest(
            number=56,
            title="t",
            html_url="http://pr",
            state="open",
            merged=False,
            body="",
        ),
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pull_request_review(review, session)

    assert out.get("ok") is True
    assert out.get("skipped") is not True
    assert fake.status_calls == [("task-100", "qa_rejected")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_pull_request_review_changes_requested_by_non_qa_skipped(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky(qa_field="fld_qa", qa_plaky_id="qa-plaky-1")
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        members=[TeamMember(id="qa-plaky-1", github_login="qa-engineer", display="QE", qa_tier=3)],
    )
    monkeypatch.setattr("boardman.services.pr_review_handler.load_team_assignments", lambda: cfg)

    async def _wrong_plaky_id(_gh: dict, **_k):
        return "not-the-assigned-qa"

    monkeypatch.setattr(
        "boardman.services.pr_review_handler.resolve_github_user_to_plaky_user_id",
        _wrong_plaky_id,
    )

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=57,
                plaky_task_id="task-101",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    review = PullRequestReviewEventPayload(
        action="submitted",
        review=GitHubReview(user={"login": "some-dev"}, state="changes_requested"),
        pull_request=GitHubPullRequest(
            number=57,
            title="t",
            html_url="http://pr",
            state="open",
            merged=False,
            body="",
        ),
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pull_request_review(review, session)

    assert out.get("ok") is True
    assert out.get("skipped") is True
    assert fake.status_calls == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_pr_merged_sets_completed_on_plaky(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky()
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_handler.PlakyClient", lambda: fake)

    async def _no_open(*_a, **_k):
        return False

    monkeypatch.setattr("boardman.services.pr_handler.has_any_open_pr_for_task", _no_open)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=77,
                plaky_task_id="task-merge",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    pr = GitHubPullRequest(
        number=77,
        title="t",
        html_url="http://pr/77",
        state="closed",
        merged=True,
        body="",
    )
    payload = PullRequestEventPayload(
        action="closed",
        pull_request=pr,
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pr_merged(payload, session)

    assert out.get("ok") is True
    assert ("task-merge", "completed") in fake.status_calls
    await engine.dispose()


@pytest.mark.asyncio
async def test_pr_review_requested_moves_to_in_qa(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky()
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_handler.PlakyClient", lambda: fake)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=88,
                plaky_task_id="task-rev",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    pr = GitHubPullRequest(
        number=88,
        title="t",
        html_url="http://pr",
        state="open",
        merged=False,
        body="",
    )
    payload = PullRequestEventPayload(
        action="review_requested",
        pull_request=pr,
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pr_review_requested(payload, session)

    assert out.get("ok") is True
    assert fake.status_calls == [("task-rev", "in_qa")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_pr_review_comment_assigned_qa_moves_in_qa_fuzzy_link(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky(qa_field="fld_qa", qa_plaky_id="qa-42")
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_handler.PlakyClient", lambda: fake)

    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        members=[
            TeamMember(id="qa-42", github_login="qaengineer", display="QE", qa_tier=3),
        ],
    )
    monkeypatch.setattr("boardman.services.pr_handler.load_team_assignments", lambda: cfg)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=99,
                plaky_task_id="task-fuzzy",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    pr = GitHubPullRequest(
        number=99,
        title="no issue in body",
        html_url="http://pr/99",
        state="open",
        merged=False,
        body="no Fixes here",
    )
    payload = PullRequestReviewCommentEventPayload(
        action="created",
        comment={"user": {"login": "qaengineer"}, "body": "lgtm"},
        pull_request=pr,
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_pr_review_comment(payload, session)

    assert out.get("ok") is True
    assert fake.status_calls == [("task-fuzzy", "in_qa")]
    assert any("qaengineer" in c[1] for c in fake.comments)
    await engine.dispose()


@pytest.mark.asyncio
async def test_issue_comment_assignee_moves_in_qa(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky()
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    async def _participants(*_a, **_k):
        return {"alice"}

    monkeypatch.setattr(
        "boardman.services.pr_review_handler.fetch_pr_assignees_and_reviewers_logins",
        _participants,
    )

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=12,
                plaky_task_id="task-ic",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    payload = IssueCommentEventPayload(
        action="created",
        issue=IssueCommentIssuePayload(number=12, pull_request={"url": "http://api/github.com"}),
        comment={"user": {"login": "alice"}, "body": "checking in"},
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_issue_comment_on_pr(payload, session)

    assert out.get("ok") is True
    assert fake.status_calls == [("task-ic", "in_qa")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_issue_comment_plaky_assigned_qa_moves_in_qa_without_github_participant(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    """Plaky QA field matches commenter's roster login → IN QA even if not GitHub assignee/reviewer."""
    fake = RecordingPlaky(qa_field="fld_qa", qa_plaky_id="qa-plaky-77")
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    async def _no_participants(*_a, **_k):
        return set()

    monkeypatch.setattr(
        "boardman.services.pr_review_handler.fetch_pr_assignees_and_reviewers_logins",
        _no_participants,
    )

    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        members=[
            TeamMember(id="qa-plaky-77", github_login="qa-roster-user", display="QE", qa_tier=3),
        ],
    )
    monkeypatch.setattr("boardman.services.pr_review_handler.load_team_assignments", lambda: cfg)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=31,
                plaky_task_id="task-qa-ic",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    payload = IssueCommentEventPayload(
        action="created",
        issue=IssueCommentIssuePayload(number=31, pull_request={"url": "http://api/github.com"}),
        comment={"user": {"login": "qa-roster-user"}, "body": "starting QA"},
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_issue_comment_on_pr(payload, session)

    assert out.get("ok") is True
    assert fake.status_calls == [("task-qa-ic", "in_qa")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_issue_comment_skips_when_not_participant_and_not_plaky_qa(
    monkeypatch: pytest.MonkeyPatch, qa_settings: None
):
    fake = RecordingPlaky(qa_field="fld_qa", qa_plaky_id="qa-plaky-77")
    _patch_task_mutations_plaky(monkeypatch, fake)
    monkeypatch.setattr("boardman.services.pr_review_handler.PlakyClient", lambda: fake)

    async def _participants(*_a, **_k):
        return {"alice"}

    monkeypatch.setattr(
        "boardman.services.pr_review_handler.fetch_pr_assignees_and_reviewers_logins",
        _participants,
    )

    cfg = TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        members=[
            TeamMember(id="qa-plaky-77", github_login="qa-roster-user", display="QE", qa_tier=3),
        ],
    )
    monkeypatch.setattr("boardman.services.pr_review_handler.load_team_assignments", lambda: cfg)

    engine, factory = await _memory_session_factory()
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="svc",
                github_pr_number=32,
                plaky_task_id="task-skip",
                github_issue_number=0,
                link_source="auto_link",
            )
        )
        await session.commit()

    payload = IssueCommentEventPayload(
        action="created",
        issue=IssueCommentIssuePayload(number=32, pull_request={"url": "http://api/github.com"}),
        comment={"user": {"login": "random-dev"}, "body": "hi"},
        repository=GitHubRepository(full_name="deepiri-org/svc", name="svc"),
    )

    async with factory() as session:
        out = await handle_issue_comment_on_pr(payload, session)

    assert out.get("skipped") is True
    assert fake.status_calls == []
    await engine.dispose()
