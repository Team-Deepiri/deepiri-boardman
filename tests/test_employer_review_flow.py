"""Employer review requirements: QA exclusion list, PR-time QA assignment,
issue-create defaults (no QA, NEEDS ASSIGNED, auto priority, Type never 'Task')."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.assignment.config import DEFAULT_QA_EXCLUDED, TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.assignment.repo_rules import QaRepoRules
from boardman.assignment import qa_picker as qp
from boardman.database.models import Base
from boardman.github.webhooks import IssueEventPayload
from boardman.services import issue_handler as ih
from boardman.services import pr_handler as ph
from boardman.services.priority_rules import infer_priority_from_text


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _member(mid: str, display: str = "", login: str = "") -> TeamMember:
    return TeamMember(
        id=mid,
        display=display or mid,
        github_login=login or mid,
        roles=["qa"],
        tier="standard",
        qa_tier=3,
        repo_globs=["team-deepiri/*"],
        weight=1.0,
    )


def _cfg(members: list[TeamMember], excluded: list[str] | None = None) -> TeamAssignmentsConfig:
    return TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        tiers={"standard": TierSpec("standard", 1.0)},
        members=members,
        heavy_repo_patterns=[],
        qa_repo_rules=QaRepoRules(),
        random_jitter=0.0,
        qa_excluded=excluded if excluded is not None else list(DEFAULT_QA_EXCLUDED),
    )


# --- exclusion list ---------------------------------------------------------------


def test_default_exclusion_list_names() -> None:
    assert set(DEFAULT_QA_EXCLUDED) == {
        "Joe Black",
        "Austin Heitzman",
        "Devin Gamble",
        "Sean San",
        "Nathan Adams",
    }


@pytest.mark.asyncio
async def test_excluded_leads_are_never_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    austin = _member("qa-austin", display="Austin Heitzman", login="austinm2h35-sketch")
    joe = _member("qa-joe", display="Joe Black", login="joeblack")
    worker = _member("qa-worker", display="Regular QA", login="regular-qa")
    cfg = _cfg([austin, joe, worker])

    async def fake_fits(candidates: Any, full_name: str):
        # Give the EXCLUDED lead the best fit — the filter must still win.
        return {m.id: (0.99 if m.id == "qa-austin" else 0.2, "d") for m in candidates}

    async def fake_tier(fn: str) -> int:
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("Team-Deepiri/some-repo", cfg)
    assert qid == "qa-worker", why


@pytest.mark.asyncio
async def test_exclusion_matches_github_login_too(monkeypatch: pytest.MonkeyPatch) -> None:
    lead = _member("qa-lead", display="S. Santos", login="sean san")
    worker = _member("qa-worker", display="Regular QA", login="regular-qa")
    cfg = _cfg([lead, worker])

    async def fake_fits(candidates: Any, full_name: str):
        return None

    async def fake_tier(fn: str) -> int:
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, _ = await qp.pick_qa_for_repo("Team-Deepiri/some-repo", cfg)
    assert qid == "qa-worker"


@pytest.mark.asyncio
async def test_all_excluded_returns_clear_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    only_lead = _member("qa-austin", display="Austin Heitzman")
    cfg = _cfg([only_lead])

    async def fake_tier(fn: str) -> int:
        return 2

    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("Team-Deepiri/some-repo", cfg)
    assert qid is None and "qa_excluded" in why


# --- priority inference -----------------------------------------------------------


def test_priority_high_medium_low() -> None:
    assert infer_priority_from_text("Login crashes on start") == "High"
    assert infer_priority_from_text("Security vulnerability in auth") == "High"
    assert infer_priority_from_text("Fix typo in README") == "Low"
    assert infer_priority_from_text("Add export button") == "Medium"
    assert infer_priority_from_text("anything", labels=["priority: high"]) == "High"
    assert infer_priority_from_text("urgent crash", labels=["low"]) == "Low"  # label wins


# --- issue create defaults --------------------------------------------------------


def _issue_payload(labels: list[dict] | None = None) -> IssueEventPayload:
    return IssueEventPayload(
        action="opened",
        issue={
            "number": 12,
            "title": "App crashes when uploading",
            "body": "Stack trace attached",
            "html_url": "https://github.com/o/r/issues/12",
            "labels": labels or [],
        },
        repository={"full_name": "o/r", "name": "r"},
    )


@pytest.mark.asyncio
async def test_issue_create_has_no_qa_and_sets_defaults(db_session, monkeypatch) -> None:
    created: dict[str, Any] = {}
    updates: list[Any] = []

    class FakePlaky:
        def __init__(self) -> None:
            pass

        async def create_task(self, **kw: Any) -> dict:
            created.update(kw)
            return {"ok": True, "task": {"id": "t-77"}, "task_url": None}

        async def add_comment(self, *a: Any, **kw: Any) -> dict:
            return {"ok": True}

    class R:
        plaky_board_id = "b1"
        plaky_group_id = "g1"
        plaky_table = ""
        category = ""
        tier = 0

    async def fake_routing(*a: Any, **kw: Any) -> R:
        return R()

    async def fake_group(bid: str, repo: str, **kw: Any) -> str:
        return "g1"

    async def fake_resolve(bid: str, *, intent: str):
        assert intent == "workflow_needs_assigned"
        return ("status-6", "0")

    async def fake_update(task_id: str, inp: Any) -> dict:
        updates.append((task_id, inp))
        return {"ok": True}

    monkeypatch.setattr(ih, "PlakyClient", FakePlaky)
    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    monkeypatch.setattr(ih, "resolve_group_for_repo", fake_group)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)

    res = await ih.handle_issue_opened(_issue_payload(labels=[{"name": "bug"}]), db_session)
    assert res["ok"] is True and res["plaky_task_id"] == "t-77"

    # No QA person field at creation:
    fv = created.get("field_values") or {}
    assert "fld_qa" not in fv and not any("person" in str(k) for k in fv)
    # Priority inferred High (crash):
    assert created.get("priority") == "high"
    # Post-create defaults: NEEDS ASSIGNED + Type from the bug label (never 'Task'):
    assert updates, "expected a post-create defaults patch"
    _tid, inp = updates[0]
    assert inp.status == "0" and inp.status_plaky_field_key == "status-6"
    assert inp.task_type == "Bug"


@pytest.mark.asyncio
async def test_issue_create_type_defaults_to_feature(db_session, monkeypatch) -> None:
    created: dict[str, Any] = {}
    updates: list[Any] = []

    class FakePlaky:
        def __init__(self) -> None:
            pass

        async def create_task(self, **kw: Any) -> dict:
            created.update(kw)
            return {"ok": True, "task": {"id": "t-78"}, "task_url": None}

    async def fake_routing(*a: Any, **kw: Any) -> None:
        return None

    async def fake_update(task_id: str, inp: Any) -> dict:
        updates.append(inp)
        return {"ok": True}

    monkeypatch.setattr(ih, "PlakyClient", FakePlaky)
    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)

    payload = IssueEventPayload(
        action="opened",
        issue={
            "number": 13,
            "title": "Add CSV export",
            "html_url": "https://github.com/o/r/issues/13",
        },
        repository={"full_name": "o/r", "name": "r"},
    )
    res = await ih.handle_issue_opened(payload, db_session)
    assert res["ok"] is True
    assert updates and updates[0].task_type == "Feature"
    assert created.get("priority") == "medium"


# --- PR-time QA assignment --------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_qa_for_pr_full_path(monkeypatch: pytest.MonkeyPatch) -> None:
    comments: list[tuple[str, int, str]] = []
    reviewers: list[tuple[str, int, list[str]]] = []
    updates: list[Any] = []

    async def fake_pick(repo_full: str):
        return "plaky-42", "qa=Regular QA ranking[...]"

    async def fake_key(bid: str, fallback: str) -> str:
        return "person-4"

    async def fake_current(plaky: Any, bid: str, tid: str, key: str) -> str:
        return ""

    async def fake_update(tid: str, inp: Any) -> dict:
        updates.append((tid, inp))
        return {"ok": True}

    async def fake_comment(full: str, num: int, body: str) -> dict:
        comments.append((full, num, body))
        return {"ok": True, "status": 201}

    async def fake_reviewers(full: str, num: int, logins: list[str]) -> dict:
        reviewers.append((full, num, logins))
        return {"ok": True, "status": 201}

    worker = _member("plaky-42", display="Regular QA", login="regular-qa")
    monkeypatch.setattr("boardman.assignment.qa_picker.pick_qa_for_repo", fake_pick)
    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_qa_assignee_field_key", fake_key
    )
    monkeypatch.setattr(ph, "_current_person_field_value", fake_current)
    monkeypatch.setattr(ph, "update_task_internal", fake_update)
    monkeypatch.setattr("boardman.github.pr_actions.comment_on_pr", fake_comment)
    monkeypatch.setattr("boardman.github.pr_actions.request_reviewers", fake_reviewers)
    monkeypatch.setattr(ph, "load_team_assignments", lambda: _cfg([worker]))

    out = await ph._assign_qa_for_pr(
        object(),
        task_id="t-1",
        board_id="b1",
        repo_full="Team-Deepiri/r",
        pr_number=9,
        pr_author_login="someone-else",
    )
    assert out["plaky_qa"]["id"] == "plaky-42" and out["plaky_qa"]["ok"] is True
    assert updates and updates[0][1].qa_plaky_id == "plaky-42"
    assert comments and "@regular-qa" in comments[0][2]
    assert reviewers == [("Team-Deepiri/r", 9, ["regular-qa"])]


@pytest.mark.asyncio
async def test_assign_qa_for_pr_skips_when_already_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_key(bid: str, fallback: str) -> str:
        return "person-4"

    async def fake_current(plaky: Any, bid: str, tid: str, key: str) -> str:
        return "existing-qa"

    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_qa_assignee_field_key", fake_key
    )
    monkeypatch.setattr(ph, "_current_person_field_value", fake_current)
    monkeypatch.setattr(ph, "load_team_assignments", lambda: _cfg([]))

    out = await ph._assign_qa_for_pr(
        object(), task_id="t-1", board_id="b1", repo_full="o/r", pr_number=9
    )
    assert out["skipped"] == "qa_already_assigned"
