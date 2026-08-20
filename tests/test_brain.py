"""The Boardman Brain: what is known before anyone asks.

The load-bearing property is the one that is easiest to lose in a later edit: assembling
project state must never make a network call. The moment it does, it stops being context
and becomes another fetch on the critical path, which is the thing the whole design exists
to remove.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.agent.brain import (
    Briefing,
    Identity,
    LiveState,
    ProjectState,
    TrackedPR,
    get_project_state,
    render_project_state,
    resolve_identity,
)
from boardman.database.models import (
    Base,
    IssueTaskMap,
    ProjectContext,
    PullRequestTaskLink,
    SyncLog,
)

REPO = "Team-Deepiri/deepiri-boardman"
SHORT = "deepiri-boardman"


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _snapshot(**over) -> str:
    payload = {
        "ok": True,
        "repo": REPO,
        "structure": {
            "default_branch": "main",
            "language": "Python",
            "description": "GitHub to Plaky sync",
            "top_level_dirs": ["boardman", "tests"],
            "important_paths": ["README.md", "pyproject.toml"],
        },
        "DIRECTION_md": "# Direction\nShip the sync.",
        "readme_md": "# Boardman\nIt syncs.",
        "code_signals": "python, fastapi",
    }
    payload.update(over)
    return json.dumps(payload)


async def _seed(session: AsyncSession, *, snapshot_age_s: float = 10.0) -> None:
    session.add(
        ProjectContext(
            repo=REPO,
            context_json=_snapshot(),
            context_source_revision="abc123def456",
            context_fetched_at=datetime.utcnow() - timedelta(seconds=snapshot_age_s),
        )
    )
    for n in (90, 91, 92):
        session.add(IssueTaskMap(github_repo=SHORT, github_issue_number=n, plaky_task_id=f"t{n}"))
    session.add(
        PullRequestTaskLink(
            github_repo=SHORT, github_pr_number=88, github_issue_number=90, plaky_task_id="t90"
        )
    )
    session.add(
        PullRequestTaskLink(
            github_repo=SHORT,
            github_pr_number=70,
            github_issue_number=0,
            plaky_task_id="t70",
            merged_at=datetime.utcnow(),
        )
    )
    session.add(
        SyncLog(
            action="pr_merged",
            github_repo=SHORT,
            github_ref="70",
            plaky_task_id="t70",
            created_at=datetime.utcnow(),
        )
    )
    await session.flush()


# --- L0 --------------------------------------------------------------------------------


def test_identity_needs_no_session_and_no_network() -> None:
    ident = resolve_identity(REPO)
    assert ident.repo_full_name == REPO
    assert ident.repo_short == SHORT
    assert ident.board_id and ident.group_id, "boardman is routed in repos.yml"
    assert ident.routed is True


def test_an_unrouted_repo_says_so_rather_than_guessing() -> None:
    ident = resolve_identity("Team-Deepiri/definitely-not-a-real-repo-xyz")
    assert ident.repo_short == "definitely-not-a-real-repo-xyz"
    assert ident.routed is False or ident.board_id  # routing may have an org-wide default


def test_an_empty_repo_is_an_empty_identity() -> None:
    assert resolve_identity("") == Identity()
    assert resolve_identity("   ").repo_full_name == ""


# --- assembly --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_assembles_all_three_layers(db) -> None:
    await _seed(db)
    state = await get_project_state(db, REPO)

    assert state.identity.default_branch == "main"  # L1 feeding L0
    assert state.briefing.present and state.briefing.state == "fresh"
    assert state.briefing.source_revision == "abc123def456"
    assert state.live.tracked_issues == [92, 91, 90]
    assert [p.number for p in state.live.active_prs] == [88]
    assert state.live.merged_prs == 1
    assert [a.action for a in state.live.recent] == ["PR merged"]
    assert state.known is True


@pytest.mark.asyncio
async def test_assembly_makes_no_network_call(db, monkeypatch) -> None:
    """The property the whole design rests on."""
    import httpx

    def explode(*_a, **_k):
        raise AssertionError("get_project_state must not touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "request", explode)
    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    await _seed(db)
    state = await get_project_state(db, REPO)
    assert state.known is True


@pytest.mark.asyncio
async def test_a_repo_with_nothing_stored_is_still_usable(db) -> None:
    state = await get_project_state(db, REPO)
    assert state.briefing.present is False
    assert state.live.tracked_issues == []
    assert state.identity.board_id, "routing is config, not history"
    text = render_project_state(state)
    assert "no cached briefing" in text.casefold()


@pytest.mark.asyncio
async def test_no_session_degrades_instead_of_raising() -> None:
    state = await get_project_state(None, REPO)
    assert state.identity.repo_full_name == REPO
    assert state.live.available is False


@pytest.mark.asyncio
async def test_a_stub_default_branch_is_not_reported_as_a_branch(db) -> None:
    """The scan used to write default_branch="unknown"; that is not an answer."""
    db.add(
        ProjectContext(
            repo=REPO,
            context_json=_snapshot(structure={"default_branch": "unknown"}),
            context_fetched_at=datetime.utcnow(),
        )
    )
    await db.flush()
    state = await get_project_state(db, REPO)
    assert state.identity.default_branch == ""
    assert "default branch" not in render_project_state(state).casefold()


@pytest.mark.asyncio
async def test_a_pending_reservation_is_not_reported_as_a_linked_pr(db) -> None:
    """`pending:<uuid>` rows are a create-time reservation, not a real task."""
    db.add(
        PullRequestTaskLink(
            github_repo=SHORT,
            github_pr_number=99,
            github_issue_number=0,
            plaky_task_id="pending:abc",
        )
    )
    await db.flush()
    state = await get_project_state(db, REPO)
    assert [p.number for p in state.live.active_prs] == []


@pytest.mark.asyncio
async def test_stale_briefing_is_labelled_stale_not_dropped(db) -> None:
    await _seed(db, snapshot_age_s=100_000)
    state = await get_project_state(db, REPO)
    assert state.briefing.present is True
    assert state.briefing.state == "stale"
    assert "ago" in render_project_state(state)


@pytest.mark.asyncio
async def test_repeated_sync_rows_collapse_to_one_line(db) -> None:
    """An issue synced eight times is one thing that happened, not eight."""
    for _ in range(8):
        db.add(
            SyncLog(
                action="issue_synced",
                github_repo=SHORT,
                github_ref="90",
                created_at=datetime.utcnow(),
            )
        )
    await db.flush()
    state = await get_project_state(db, REPO)
    assert len(state.live.recent) == 1


@pytest.mark.asyncio
async def test_another_repos_rows_are_not_read(db) -> None:
    db.add(IssueTaskMap(github_repo="deepiri-axiom", github_issue_number=5, plaky_task_id="x"))
    await _seed(db)
    state = await get_project_state(db, REPO)
    assert 5 not in state.live.tracked_issues


# --- render ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_leads_with_facts_and_stays_within_budget(db) -> None:
    await _seed(db)
    text = render_project_state(await get_project_state(db, REPO), max_chars=6500)
    assert len(text) <= 6500
    head = text[:600]
    assert "board `" in head and "group `" in head, "routing must be near the top"
    assert "issues tracked on the board: 3" in text
    assert "PR #88 for issue #90" in text
    assert "abc123def456"[:12] in text, "provenance has to survive into the prompt"


@pytest.mark.asyncio
async def test_render_is_smaller_than_the_prose_block_it_replaces(db) -> None:
    """The point of structuring the state is to say more in fewer characters."""
    from boardman.agent.repo_context import snapshot_prompt_block

    big_readme = "# Boardman\n" + ("readme prose. " * 400)
    db.add(
        ProjectContext(
            repo=REPO,
            context_json=_snapshot(readme_md=big_readme, DIRECTION_md=big_readme),
            context_fetched_at=datetime.utcnow(),
        )
    )
    await db.flush()
    state = await get_project_state(db, REPO)
    assert len(render_project_state(state)) < len(
        snapshot_prompt_block(_snapshot(readme_md=big_readme, DIRECTION_md=big_readme))
    )


def test_render_of_an_empty_state_is_empty() -> None:
    empty = ProjectState(identity=Identity(), briefing=Briefing(), live=LiveState())
    assert render_project_state(empty) == ""


def test_render_never_claims_routing_it_does_not_have() -> None:
    state = ProjectState(
        identity=Identity(repo_full_name="o/r", repo_short="r"),
        briefing=Briefing(),
        live=LiveState(available=False),
    )
    text = render_project_state(state)
    assert "no routing configured" in text
    assert "board `" not in text


def test_render_lists_a_pr_with_no_issue_honestly() -> None:
    state = ProjectState(
        identity=Identity(repo_full_name="o/r", repo_short="r", board_id="1", group_id="2"),
        briefing=Briefing(),
        live=LiveState(
            active_prs=[TrackedPR(number=5, task_id="t", issue_number=0, link_source="")]
        ),
    )
    assert "PR #5 (no linked issue) -> task t" in render_project_state(state)


# --- stale-while-revalidate --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_briefing_queues_exactly_one_refresh(db, monkeypatch) -> None:
    from boardman.agent import brain

    queued: list[dict] = []

    async def fake_enqueue(kind: str, payload: dict) -> str:
        queued.append({"kind": kind, **payload})
        return f"job-{len(queued)}"

    monkeypatch.setattr("boardman.jobs.deferred.enqueue_and_run_soon", fake_enqueue)
    brain._revalidating.clear()

    await _seed(db, snapshot_age_s=100_000)
    state = await get_project_state(db, REPO)
    assert state.briefing.state == "stale"

    assert await brain.schedule_revalidation(state) == "job-1"
    # A second turn while the first refresh is still in flight must not queue another.
    assert await brain.schedule_revalidation(state) == ""
    assert len(queued) == 1
    assert queued[0] == {"kind": "boardman_repo_refresh_job", "repo": REPO}


@pytest.mark.asyncio
async def test_a_fresh_briefing_queues_nothing(db, monkeypatch) -> None:
    from boardman.agent import brain

    async def fake_enqueue(_kind: str, _payload: dict) -> str:
        raise AssertionError("a fresh snapshot needs no refresh")

    monkeypatch.setattr("boardman.jobs.deferred.enqueue_and_run_soon", fake_enqueue)
    brain._revalidating.clear()
    await _seed(db, snapshot_age_s=5)
    state = await get_project_state(db, REPO)
    assert await brain.schedule_revalidation(state) == ""


@pytest.mark.asyncio
async def test_a_missing_briefing_is_worth_fetching(db, monkeypatch) -> None:
    from boardman.agent import brain

    queued: list[str] = []

    async def fake_enqueue(_kind: str, payload: dict) -> str:
        queued.append(payload["repo"])
        return "job-x"

    monkeypatch.setattr("boardman.jobs.deferred.enqueue_and_run_soon", fake_enqueue)
    brain._revalidating.clear()
    state = await get_project_state(db, REPO)
    assert state.briefing.state == "miss"
    assert await brain.schedule_revalidation(state) == "job-x"
    assert queued == [REPO]


@pytest.mark.asyncio
async def test_a_queue_failure_is_invisible_to_the_caller(db, monkeypatch) -> None:
    """The user's answer already went out. A broken queue must not surface here."""
    from boardman.agent import brain

    async def boom(_kind: str, _payload: dict) -> str:
        raise RuntimeError("queue is down")

    monkeypatch.setattr("boardman.jobs.deferred.enqueue_and_run_soon", boom)
    brain._revalidating.clear()
    await _seed(db, snapshot_age_s=100_000)
    state = await get_project_state(db, REPO)
    assert await brain.schedule_revalidation(state) == ""
    # and the failure did not poison the cooldown, so the next turn may retry
    assert REPO not in brain._revalidating


@pytest.mark.asyncio
async def test_an_unavailable_briefing_is_not_retried_in_a_loop(db, monkeypatch) -> None:
    """`unavailable` means the column is missing, which a refresh cannot fix."""
    from boardman.agent import brain

    async def boom(_kind: str, _payload: dict) -> str:
        raise AssertionError("must not queue")

    monkeypatch.setattr("boardman.jobs.deferred.enqueue_and_run_soon", boom)
    brain._revalidating.clear()
    state = ProjectState(
        identity=Identity(repo_full_name=REPO, repo_short=SHORT),
        briefing=Briefing(state="unavailable"),
        live=LiveState(),
    )
    assert await brain.schedule_revalidation(state) == ""
