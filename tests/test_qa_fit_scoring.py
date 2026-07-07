"""GitHub-fit QA scoring: cosine math, ranked choice, rules filter, legacy fallback."""

from __future__ import annotations

import pytest

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.assignment.repo_rules import QaRepoRules
from boardman.assignment import qa_picker as qp
from boardman.github.qa_contribution_profile import (
    QaContributionProfile,
    RepoInfo,
    cosine_similarity,
    direct_contribution_score,
)


def _cfg(members: list[TeamMember]) -> TeamAssignmentsConfig:
    return TeamAssignmentsConfig(
        plaky_field_qa="fld_qa",
        tiers={"standard": TierSpec("standard", 1.0)},
        members=members,
        heavy_repo_patterns=[],
        qa_repo_rules=QaRepoRules(),
        random_jitter=0.0,
    )


def _member(mid: str, login: str = "", qa_tier: int = 3, weight: float = 1.0) -> TeamMember:
    return TeamMember(
        id=mid,
        display=mid,
        github_login=login or mid,
        roles=["qa"],
        tier="standard",
        qa_tier=qa_tier,
        repo_globs=["deepiri-org/*", "team-deepiri/*"],
        weight=weight,
    )


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity({}, {"a": 1.0}) == 0.0
    assert cosine_similarity({"a": 2.0}, {"a": 1.0}) == pytest.approx(1.0)
    assert cosine_similarity({"a": 1.0}, {"b": 1.0}) == 0.0
    mixed = cosine_similarity({"a": 1.0, "b": 1.0}, {"a": 1.0})
    assert 0.6 < mixed < 0.8


def test_direct_contribution_score_saturates() -> None:
    p = QaContributionProfile(login="x", repo_weights={"o/target": 3.0})
    assert direct_contribution_score(p, "o/other") == 0.0
    s = direct_contribution_score(p, "O/Target")
    assert 0.9 < s < 1.0


def test_repo_info_tokens_weighting() -> None:
    info = RepoInfo(
        full_name="org/deepiri-cyrex",
        topics=["llm", "agents"],
        description="Cyrex agent runtime",
    )
    toks = info.tokens()
    assert toks["deepiri"] >= 2.0 and toks["cyrex"] >= 2.0
    assert toks["llm"] == pytest.approx(1.5)


def test_ranked_choice_prefers_higher_fit() -> None:
    a, b = _member("qa-a"), _member("qa-b")
    cfg = _cfg([a, b])
    fits = {"qa-a": (0.9, "strong"), "qa-b": (0.1, "weak")}
    winner, detail = qp._ranked_choice([a, b], cfg, fits)
    assert winner is not None and winner.id == "qa-a"
    assert "ranking[" in detail and "qa-a" in detail


def test_ranked_choice_weight_breaks_zero_fit_ties() -> None:
    a, b = _member("qa-a", weight=0.5), _member("qa-b", weight=2.0)
    cfg = _cfg([a, b])
    winner, _ = qp._ranked_choice([a, b], cfg, {})
    assert winner is not None and winner.id == "qa-b"


@pytest.mark.asyncio
async def test_pick_uses_scored_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _member("qa-a"), _member("qa-b")
    cfg = _cfg([a, b])

    async def fake_fits(candidates, full_name):
        return {"qa-b": (0.8, "direct=0.9"), "qa-a": (0.05, "direct=0.0")}

    async def fake_tier(fn):
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("deepiri-org/some-repo", cfg)
    assert qid == "qa-b", why
    assert "ranking[" in why


@pytest.mark.asyncio
async def test_pick_falls_back_to_legacy_when_fit_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    a = _member("qa-a")
    cfg = _cfg([a])

    async def fake_fits(candidates, full_name):
        return None

    async def fake_tier(fn):
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("deepiri-org/some-repo", cfg)
    assert qid == "qa-a"
    assert "legacy weighted pick" in why


@pytest.mark.asyncio
async def test_qa_repo_rules_now_filter_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """tier1_only / tier2_excluded patterns from team_assignments.yml are enforced."""
    t2 = _member("qa-t2", qa_tier=2)
    t3 = _member("qa-t3", qa_tier=3)
    cfg = _cfg([t2, t3])
    cfg.qa_repo_rules = QaRepoRules(tier2_excluded_patterns=["*restricted*"])

    async def fake_fits(candidates, full_name):
        # Give the tier-2 member the better fit — the rules filter must still win.
        return {m.id: (0.9 if m.id == "qa-t2" else 0.2, "d") for m in candidates}

    async def fake_tier(fn):
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("deepiri-org/restricted-repo", cfg)
    assert qid == "qa-t3", why
