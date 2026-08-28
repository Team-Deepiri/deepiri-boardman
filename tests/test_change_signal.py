"""Event-driven cache invalidation, on both dispatch paths.

There are two independent copies of the GitHub dispatch table — the webhook route and the
poller — and they already differ from each other. A hook wired into only one of them is
the worst outcome: the tests exercise the route and pass, while the live poller session
serves context that is quietly a day out of date.

These tests also pin the rule that matters more than the caching: invalidation runs after
the sync has committed and can never change what the sync returned.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base
from boardman.github import read_cache
from boardman.github.change_signal import note_repo_changed, repo_full_name_from_payload

REPO = {"full_name": "Team-Deepiri/deepiri-boardman", "name": "deepiri-boardman"}


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_cache():
    read_cache.clear_read_cache()
    yield
    read_cache.clear_read_cache()


def _seed_boardman_cache() -> None:
    for key in (
        "structure:team-deepiri/deepiri-boardman",
        "planning:team-deepiri/deepiri-boardman:20",
        "repo-tree:Team-Deepiri/deepiri-boardman@main",
        "file:Team-Deepiri/deepiri-boardman@main:README.md",
    ):
        read_cache._entries[key] = (1.0e9, "cached")


# --- the helper ------------------------------------------------------------------------


def test_full_name_is_read_from_a_dict_payload() -> None:
    assert repo_full_name_from_payload({"repository": REPO}) == "Team-Deepiri/deepiri-boardman"


def test_full_name_is_read_from_a_parsed_model() -> None:
    from boardman.github.webhooks import IssueEventPayload

    payload = IssueEventPayload(
        action="opened",
        issue={"number": 1, "title": "t", "html_url": "u", "labels": []},
        repository=REPO,
    )
    assert repo_full_name_from_payload(payload) == "Team-Deepiri/deepiri-boardman"


def test_a_payload_with_no_repository_is_not_an_error() -> None:
    assert repo_full_name_from_payload({}) == ""
    assert note_repo_changed("") == 0


def test_an_unrelated_event_type_changes_nothing() -> None:
    """A star or a watch says nothing about the code or the work."""
    _seed_boardman_cache()
    assert note_repo_changed(REPO["full_name"], event="star") == 0
    assert read_cache.cache_stats()["entries"] == 4


@pytest.mark.parametrize("event", ["push", "issues", "pull_request", "pull_request_review"])
def test_events_that_move_the_repo_do_invalidate(event: str) -> None:
    _seed_boardman_cache()
    assert note_repo_changed(REPO["full_name"], event=event) == 4
    assert read_cache.cache_stats()["entries"] == 0


def test_an_unknown_event_name_still_invalidates() -> None:
    """The poller passes no event name. Unknown means "something happened", and being
    conservative there costs one refetch; being wrong costs a false answer."""
    _seed_boardman_cache()
    assert note_repo_changed(REPO["full_name"]) == 4


def test_a_broken_cache_never_propagates_into_a_sync_handler(monkeypatch) -> None:
    def boom(_name: str) -> int:
        raise RuntimeError("cache exploded")

    monkeypatch.setattr("boardman.github.read_cache.invalidate_repo", boom)
    assert note_repo_changed(REPO["full_name"], event="push") == 0  # swallowed, not raised


# --- the webhook route path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_webhook_dispatch_invalidates_and_returns_the_same_result(
    db_session, monkeypatch
) -> None:
    from boardman.routes import github_events

    async def fake_handler(_payload: Any, _session: Any) -> dict[str, Any]:
        return {"ok": True, "plaky_task_id": "7180001", "marker": "unchanged"}

    monkeypatch.setattr(github_events, "handle_issue_opened", fake_handler)
    _seed_boardman_cache()

    result = await github_events.dispatch_github_event(
        "issues",
        {
            "action": "opened",
            "issue": {"number": 5, "title": "t", "html_url": "u", "labels": []},
            "repository": REPO,
        },
        db_session,
    )

    assert result == {"ok": True, "plaky_task_id": "7180001", "marker": "unchanged"}
    assert read_cache.cache_stats()["entries"] == 0


@pytest.mark.asyncio
async def test_an_ignored_event_does_not_invalidate(db_session) -> None:
    """No handler ran, so nothing about the repo changed as far as we know."""
    from boardman.routes import github_events

    _seed_boardman_cache()
    result = await github_events.dispatch_github_event(
        "issues",
        {
            "action": "pinned",
            "issue": {"number": 5, "title": "t", "html_url": "u", "labels": []},
            "repository": REPO,
        },
        db_session,
    )
    assert result.get("message") == "Event ignored"
    assert read_cache.cache_stats()["entries"] == 4


# --- the poller path ---------------------------------------------------------------------


def test_the_poller_dispatch_also_invalidates() -> None:
    """Not a behaviour test of the poller, a structural one: the second dispatch table
    must carry the hook, because nothing else forces the two to stay in step."""
    import inspect

    from boardman.services import github_poller

    src = inspect.getsource(github_poller)
    assert (
        src.count("note_repo_changed(") >= 2
    ), "the poller needs the hook on BOTH its handler dispatch and its commit/push path"
    run_handler = inspect.getsource(github_poller.GitHubEventPoller._run_handler)
    assert "note_repo_changed(" in run_handler
    commits = inspect.getsource(github_poller.GitHubEventPoller._comment_commits)
    assert "note_repo_changed(" in commits


def test_the_hook_runs_after_the_commit_not_before() -> None:
    """Invalidating before the commit would drop the cache for a write that then rolls
    back, and worse, an exception there would fail the sync."""
    import inspect

    from boardman.services import github_poller

    src = inspect.getsource(github_poller.GitHubEventPoller._run_handler)
    commit_at = src.index("await session.commit()")
    hook_at = src.index("note_repo_changed(")
    assert commit_at < hook_at, "the invalidation hook must come after the commit"


# --- a repo appearing or disappearing changes the org listing ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["repository", "create", "delete"])
async def test_a_repository_event_forgets_the_org_listing(db_session, event: str) -> None:
    """The org listing is cached for ten minutes with no other way to learn that a repo was
    created, renamed or removed, so a new repo was invisible until the TTL happened to lapse.
    Raised by a Sorge review on PR #88."""
    from boardman.github import org_repos
    from boardman.routes import github_events

    org_repos._org_repos_cache[("team-deepiri", True)] = (1.0e12, ["Team-Deepiri/old"])
    org_repos._org_rows_cache[("team-deepiri", True)] = (
        1.0e12,
        [{"full_name": "Team-Deepiri/old"}],
    )
    _seed_boardman_cache()

    result = await github_events.dispatch_github_event(
        event, {"action": "created", "repository": REPO}, db_session
    )

    assert result["ok"] is True
    assert "invalidated" in result["message"]
    assert not org_repos._org_repos_cache, "the stale org listing must be dropped"
    assert not org_repos._org_rows_cache, "and its activity rows with it"
    assert read_cache.cache_stats()["entries"] == 0


@pytest.mark.asyncio
async def test_an_ordinary_event_leaves_the_org_listing_alone(db_session, monkeypatch) -> None:
    """Only repo-shaped events touch it; an issue does not."""
    from boardman.github import org_repos
    from boardman.routes import github_events

    async def fake_handler(_p, _s):
        return {"ok": True}

    monkeypatch.setattr(github_events, "handle_issue_opened", fake_handler)
    org_repos._org_repos_cache[("team-deepiri", True)] = (1.0e12, ["Team-Deepiri/x"])
    await github_events.dispatch_github_event(
        "issues",
        {
            "action": "opened",
            "issue": {"number": 1, "title": "t", "html_url": "u", "labels": []},
            "repository": REPO,
        },
        db_session,
    )
    assert org_repos._org_repos_cache, "an issue event says nothing about which repos exist"
    org_repos.clear_org_repos_cache()
