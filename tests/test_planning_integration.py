from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import (
    Base,
    IssueTaskMap,
    OpenPRTrack,
    ProjectContext,
    PullRequestTaskLink,
    ScanRun,
    SyncLog,
)
from boardman.planning.context_aggregator import ContextAggregator
from boardman.planning.huddle.context_direction import DirectionPlanningContext
from boardman.planning.huddle.context_github import GitHubPlanningContext, _to_summary
from boardman.planning.huddle.context_plaky import PlakyPlanningContext, _to_summary as plaky_to_summary
from boardman.planning.huddle.context_sync import SyncPlanningContext
from boardman.planning.huddle.models import MeetingRequest
from boardman.planning.huddle.plan_output import validate_meeting_plan_markdown
from boardman.planning.huddle.planner import MeetingPlanner
from boardman.planning.service import generate_plan
from boardman.planning.huddle.team_repos import repos_for_team

FIXTURES = Path(__file__).parent / "fixtures" / "planning"
TEAM_REPOS = {"qa": ["deepiri-platform"]}


def _load_fixture(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


async def _memory_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_planning_sqlite(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(
            PullRequestTaskLink(
                github_repo="deepiri-platform",
                github_pr_number=101,
                github_issue_number=55,
                plaky_task_id="plaky-item-101",
                link_source="issue_keyword",
            )
        )
        session.add(
            IssueTaskMap(
                github_repo="deepiri-platform",
                github_issue_number=55,
                plaky_task_id="plaky-item-101",
                plaky_task_url="https://plaky.example/items/plaky-item-101",
            )
        )
        session.add(
            OpenPRTrack(
                repo_full_name="Team-Deepiri/deepiri-platform",
                pr_number=101,
                plaky_item_id="plaky-item-101",
                pr_title="Fix auth middleware for session tokens",
                pr_url="https://github.com/Team-Deepiri/deepiri-platform/pull/101",
            )
        )
        session.add(
            SyncLog(
                action="pr_opened",
                github_repo="deepiri-platform",
                github_ref="101",
                plaky_task_id="plaky-item-101",
            )
        )
        session.add(
            ProjectContext(
                repo="Team-Deepiri/deepiri-platform",
                summary="Focus on auth reliability, release checklist automation, and staging stability.",
                last_scanned=datetime.utcnow(),
            )
        )
        session.add(
            ScanRun(
                github_repo="Team-Deepiri/deepiri-platform",
                tasks_proposed=json.dumps(
                    [{"title": "Add release gate"}, {"title": "Fix staging drift"}]
                ),
                tasks_created=1,
                created_at=datetime.utcnow(),
            )
        )
        await session.commit()


def _planning_db_factory() -> async_sessionmaker[AsyncSession]:
    async def _setup() -> async_sessionmaker[AsyncSession]:
        _, factory = await _memory_session_factory()
        await _seed_planning_sqlite(factory)
        return factory

    return asyncio.run(_setup())


def _github_summaries_from_fixture(team_focus: str):
    raw = _load_fixture("github_prs.json")
    repos = repos_for_team(TEAM_REPOS, team_focus)
    summaries = []
    for repo in repos:
        for item in raw.get(repo, []):
            summaries.append(_to_summary(repo, item))
    return summaries


def _plaky_summaries_from_fixture():
    raw = _load_fixture("plaky_items.json")
    return [plaky_to_summary(item, board_label="board=acceptance") for item in raw["items"]]


def _valid_llm_markdown() -> str:
    return """
## Purpose
Weekly QA sync on release readiness, regression coverage, and deployment gates for the sprint.

## Agenda Timeline
- 0:00–0:08 opening and outcomes
- 0:08–0:18 team snapshot on active streams
- 0:18–0:40 group round table on work, wins, and blockers
- 0:40–0:52 decisions and escalation paths
- 0:52–1:00 action read-back

## Group Round Table
Each participant shares what they are working on next, two wins from the week, and the top
blocker that needs a decision or owner today.

## Team Snapshot
Forum label: **QA:** core sync. Streams: regression automation, release checklist coverage,
and cross-team dependency on platform for staging stability.

## Decisions Needed
Confirm release gate owners and whether we defer non-critical defects to the next sprint.

## Risks and Blockers
Test env instability may delay sign-off; dependency risk if platform migration slips;
escalation path if incident volume spikes mid-week.

## Action Items
- [ ] Owner: QA lead — publish test matrix (due: Wed)
- [ ] Owner: Release manager — confirm rollback plan (due: Thu)

## Follow-up Checklist
- [ ] Notes posted to the shared doc
- [ ] Owners acknowledged actions in thread
"""


class _CaptureLlm:
    def __init__(self, text: str | None = None) -> None:
        self.prompt = ""
        self._text = text or _valid_llm_markdown()

    def generate(self, prompt: str):  # noqa: ANN001
        self.prompt = prompt

        class R:
            pass

        r = R()
        r.text = self._text
        r.provider = "fixture-llm"
        r.model = "fixture-model"
        return r


class _FailingLlm:
    def generate(self, prompt: str):  # noqa: ANN001
        raise RuntimeError("offline acceptance: no live LLM")


async def _never_fetch_direction(repo_full: str) -> str:
    raise AssertionError(f"direction fetch should not run in offline acceptance: {repo_full}")


def _build_aggregator(factory: async_sessionmaker[AsyncSession]) -> ContextAggregator:
    return ContextAggregator(
        github_context=GitHubPlanningContext(),
        plaky_context=PlakyPlanningContext(),
        sync_context=SyncPlanningContext(session_factory=factory, team_repos=TEAM_REPOS),
        direction_context=DirectionPlanningContext(
            session_factory=factory,
            team_repos=TEAM_REPOS,
            direction_fetcher=_never_fetch_direction,
        ),
    )


def _weekly_request() -> MeetingRequest:
    return MeetingRequest(
        meeting_title="QA Weekly Acceptance",
        meeting_type="weekly-status-sync",
        team_focus="qa",
        attendees_count=12,
        objectives=["Validate offline planning pipeline"],
        week_label="next-week",
        target_date_iso="2026-06-16",
    )


def test_offline_plan_pipeline_injects_fixture_context(monkeypatch):
    factory = _planning_db_factory()
    monkeypatch.setattr("boardman.settings.settings.github_pat", "fixture-token")
    monkeypatch.setattr("boardman.settings.settings.plaky_api_key", "fixture-key")
    monkeypatch.setattr(
        GitHubPlanningContext,
        "fetch_recent_prs",
        lambda self, team_focus: _github_summaries_from_fixture(team_focus),
    )
    monkeypatch.setattr(
        PlakyPlanningContext,
        "fetch_recent_items",
        lambda self, team_focus: _plaky_summaries_from_fixture(),
    )

    llm = _CaptureLlm()
    planner = MeetingPlanner(
        llm=llm,  # type: ignore[arg-type]
        context_aggregator=_build_aggregator(factory),
    )
    plan = planner.plan(_weekly_request())

    assert "Fix auth middleware for session tokens" in llm.prompt
    assert "Auth middleware hardening" in llm.prompt
    assert "Boardman Sync State" in llm.prompt
    assert "Repo Direction" in llm.prompt
    assert "plaky-item-101" in llm.prompt
    assert plan.provider_used == "fixture-llm"
    schema = validate_meeting_plan_markdown(plan.markdown)
    assert schema.ok, schema.errors


def test_offline_generate_plan_fallback_passes_schema(monkeypatch, tmp_path: Path):
    factory = _planning_db_factory()
    monkeypatch.setattr("boardman.planning.service.settings.planning_output_dir", str(tmp_path))
    monkeypatch.setattr("boardman.settings.settings.github_pat", "fixture-token")
    monkeypatch.setattr("boardman.settings.settings.plaky_api_key", "fixture-key")
    monkeypatch.setattr(
        GitHubPlanningContext,
        "fetch_recent_prs",
        lambda self, team_focus: _github_summaries_from_fixture(team_focus),
    )
    monkeypatch.setattr(
        PlakyPlanningContext,
        "fetch_recent_items",
        lambda self, team_focus: _plaky_summaries_from_fixture(),
    )

    aggregator = _build_aggregator(factory)
    plan = generate_plan(
        _weekly_request(),
        output_path=tmp_path / "acceptance_plan.md",
        planner=MeetingPlanner(llm=_FailingLlm(), context_aggregator=aggregator),  # type: ignore[arg-type]
    )

    assert (tmp_path / "acceptance_plan.md").exists()
    assert plan.provider_used == "deterministic-fallback"
    schema = validate_meeting_plan_markdown(plan.markdown)
    assert schema.ok, schema.errors
