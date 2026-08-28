"""GitHub-fit QA scoring: cosine math, ranked choice, rules filter, legacy fallback."""

from __future__ import annotations

import pytest

from boardman.assignment import qa_picker as qp
from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.assignment.repo_rules import QaRepoRules
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
    fits = {
        "qa-a": (0.9, qp.FitDetail(0.5, 0.7, 0.3, ["org/repo-x"])),
        "qa-b": (0.1, qp.FitDetail(0.0, 0.1, 0.0, [])),
    }
    winner, detail = qp._ranked_choice([a, b], cfg, fits)
    assert winner is not None and winner.id == "qa-a"
    assert "We picked qa-a" in detail and "Confidence:" in detail


def test_ranked_choice_weight_breaks_zero_fit_ties() -> None:
    a, b = _member("qa-a", weight=0.5), _member("qa-b", weight=2.0)
    cfg = _cfg([a, b])
    winner, reason = qp._ranked_choice([a, b], cfg, {})
    assert winner is not None and winner.id == "qa-b"
    assert "We picked qa-b" in reason


@pytest.mark.asyncio
async def test_pick_uses_scored_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _member("qa-a"), _member("qa-b")
    cfg = _cfg([a, b])

    async def fake_fits(candidates, full_name):
        return {
            "qa-b": (0.8, qp.FitDetail(0.9, 0.5, 0.3, ["org/repo-x"])),
            "qa-a": (0.05, qp.FitDetail(0.0, 0.1, 0.0, [])),
        }

    async def fake_tier(fn):
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("deepiri-org/some-repo", cfg)
    assert qid == "qa-b", why
    assert "We picked qa-b" in why and "Confidence:" in why


@pytest.mark.asyncio
async def test_pick_falls_back_to_legacy_when_fit_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert "team availability" in why


@pytest.mark.asyncio
async def test_qa_repo_rules_now_filter_the_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    """tier1_only / tier2_excluded patterns from team_assignments.yml are enforced."""
    t2 = _member("qa-t2", qa_tier=2)
    t3 = _member("qa-t3", qa_tier=3)
    cfg = _cfg([t2, t3])
    cfg.qa_repo_rules = QaRepoRules(tier2_excluded_patterns=["*restricted*"])

    async def fake_fits(candidates, full_name):
        # Give the tier-2 member the better fit — the rules filter must still win.
        return {
            m.id: (0.9 if m.id == "qa-t2" else 0.2, qp.FitDetail(0.3, 0.3, 0.3, []))
            for m in candidates
        }

    async def fake_tier(fn):
        return 2

    monkeypatch.setattr(qp, "_github_fit_scores", fake_fits)
    monkeypatch.setattr(qp, "_auto_classify_repo_tier", fake_tier)
    qid, why = await qp.pick_qa_for_repo("deepiri-org/restricted-repo", cfg)
    assert qid == "qa-t3", why


# ── Humanization unit tests ──────────────────────────────────────────────


class TestConfidencePct:
    def test_zero_fit_gives_minimum(self):
        assert qp._confidence_pct(0.0) == 20

    def test_perfect_fit_caps_at_97(self):
        assert qp._confidence_pct(1.0) == 97

    def test_moderate_fit(self):
        pct = qp._confidence_pct(0.5)
        assert 55 <= pct <= 65


class TestStrengthPhrases:
    def test_all_zeros_gives_fallback(self):
        d = qp.FitDetail(0, 0, 0, [])
        assert qp._strength_phrases(d) == ["available team member"]

    def test_high_direct(self):
        d = qp.FitDetail(0.6, 0, 0, [])
        phrases = qp._strength_phrases(d)
        assert any("active contributor" in p for p in phrases)

    def test_moderate_lang(self):
        d = qp.FitDetail(0, 0.4, 0, [])
        phrases = qp._strength_phrases(d)
        assert any("language" in p for p in phrases)

    def test_moderate_tokens(self):
        d = qp.FitDetail(0, 0, 0.2, [])
        phrases = qp._strength_phrases(d)
        assert any("similar projects" in p for p in phrases)


class TestHumanizeFitReason:
    def test_contains_name_and_role(self):
        d = qp.FitDetail(0.5, 0.6, 0.3, ["org/repo-a"])
        result = qp.humanize_fit_reason("Alice", "QA reviewer", d, 0.7, ["Bob"])
        assert "Alice" in result
        assert "QA reviewer" in result
        assert "Confidence:" in result

    def test_includes_top_repos(self):
        d = qp.FitDetail(0, 0.4, 0, ["org/repo-a", "org/repo-b"])
        result = qp.humanize_fit_reason("Alice", "developer", d, 0.3, [])
        assert "org/repo-a" in result and "org/repo-b" in result

    def test_includes_runners_up(self):
        d = qp.FitDetail(0, 0.4, 0, [])
        result = qp.humanize_fit_reason("Alice", "QA reviewer", d, 0.3, ["Bob", "Carol"])
        assert "Bob" in result and "Carol" in result

    def test_zero_fit_still_produces_paragraph(self):
        d = qp.FitDetail(0, 0, 0, [])
        result = qp.humanize_fit_reason("Alice", "QA reviewer", d, 0.0, [])
        assert "Alice" in result
        assert "best available match" in result
        assert "Confidence: 20%" in result

    def test_no_raw_scores_in_output(self):
        d = qp.FitDetail(0.3, 0.5, 0.2, ["org/repo-a"])
        result = qp.humanize_fit_reason("Alice", "QA reviewer", d, 0.5, ["Bob"])
        assert "direct=" not in result
        assert "lang=" not in result
        assert "tokens=" not in result
        assert "ranking[" not in result
        assert "fit[" not in result
