"""QA/engineer assignment picker (team_assignments.yml logic)."""

from __future__ import annotations

import random

import pytest

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.assignment.repo_rules import QaRepoRules
from boardman.assignment.qa_picker import (
    build_assignment_field_map,
    build_repo_field_map,
    github_repo_suffix_name,
    normalize_github_repo_inputs,
    pick_engineer_for_repo,
    pick_qa_for_repo,
    repo_is_heavy,
)


def _sample_cfg() -> TeamAssignmentsConfig:
    return TeamAssignmentsConfig(
        plaky_field_engineer="fld_eng",
        plaky_field_qa="fld_qa",
        tiers={
            "light": TierSpec("light", 0.8),
            "standard": TierSpec("standard", 1.0),
            "heavy": TierSpec("heavy", 1.2),
        },
        members=[
            TeamMember(
                id="qa-heavy",
                display="QA Heavy",
                roles=["qa"],
                tier="heavy",
                qa_tier=3,
                repo_globs=["deepiri-org/emotion-*"],
                explicit_repos=["deepiri-org/emotion-desktop"],
                weight=1.0,
            ),
            TeamMember(
                id="qa-light",
                display="QA Light",
                roles=["qa"],
                tier="light",
                qa_tier=2,
                repo_globs=["deepiri-org/*"],
                weight=1.0,
            ),
            TeamMember(
                id="dev-1",
                display="Dev",
                roles=["engineer"],
                repo_globs=["deepiri-org/*"],
                weight=2.0,
            ),
            TeamMember(
                id="dev-2",
                display="Dev2",
                roles=["engineer"],
                repo_globs=["deepiri-org/*"],
                weight=1.0,
            ),
        ],
        heavy_repo_patterns=["*emotion*"],
        qa_repo_rules=QaRepoRules(tier2_excluded_patterns=["*emotion*"], tier1_only_patterns=[]),
        random_jitter=0.0,
    )


def test_repo_is_heavy():
    assert repo_is_heavy("deepiri-org/emotion-desktop", ["*emotion*"]) is True
    assert repo_is_heavy("deepiri-org/boardman", ["*emotion*"]) is False


@pytest.mark.asyncio
async def test_tier2_excludes_emotion_repo_for_tier2_qa():
    cfg = _sample_cfg()
    qid, why = await pick_qa_for_repo("deepiri-org/emotion-desktop", cfg)
    assert qid == "qa-heavy", why
    assert "qa-heavy" in why or "QA Heavy" in why or "pool" in why


@pytest.mark.asyncio
async def test_non_heavy_repo_allows_light_qa_in_pool():
    cfg = _sample_cfg()
    random.seed(0)
    qid, _ = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid in ("qa-heavy", "qa-light")


def test_engineer_is_highest_weight():
    cfg = _sample_cfg()
    eid, _ = pick_engineer_for_repo("deepiri-org/boardman", cfg)
    assert eid == "dev-1"


@pytest.mark.asyncio
async def test_build_assignment_field_map():
    cfg = _sample_cfg()
    m = await build_assignment_field_map("deepiri-org/emotion-desktop", cfg)
    assert m.get("fld_eng") == "dev-1"
    assert m.get("fld_qa") == "qa-heavy"


@pytest.mark.asyncio
async def test_build_assignment_field_map_engineer_qa_key_overrides():
    """Board-inferred keys (when YAML omits plaky_field_engineer/qa) must still receive ids."""
    cfg = _sample_cfg()
    cfg.plaky_field_engineer = ""
    cfg.plaky_field_qa = ""
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        plaky_field_engineer_key="inferred_contributor",
        plaky_field_qa_key="inferred_qa",
    )
    assert m.get("inferred_contributor") == "dev-1"
    assert m.get("inferred_qa") == "qa-heavy"


@pytest.mark.asyncio
async def test_build_assignment_field_map_includes_repo():
    cfg = _sample_cfg()
    cfg.plaky_field_repo = "fld_repo"
    m = await build_assignment_field_map("deepiri-org/emotion-desktop", cfg)
    assert m.get("fld_repo") == "deepiri-org/emotion-desktop"


@pytest.mark.asyncio
async def test_build_assignment_field_map_repo_value_override():
    cfg = _sample_cfg()
    cfg.plaky_field_repo = "fld_repo"
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        repo_value="other-org/custom",
    )
    assert m.get("fld_repo") == "other-org/custom"


def test_github_repo_suffix_name():
    assert github_repo_suffix_name("Team-Deepiri/deepiri-platform") == "deepiri-platform"
    assert github_repo_suffix_name("solo-repo") == "solo-repo"


def test_build_repo_field_map_short_format_for_tag_columns():
    cfg = _sample_cfg()
    cfg.plaky_field_repo = "tag_col"
    cfg.plaky_field_github_repos = "tag_col"
    m = build_repo_field_map(
        cfg,
        github_repos=["acme/foo", "acme/bar"],
        repo_value_format="short",
        github_repos_value_format="short",
    )
    assert m.get("tag_col") == "foo, bar"


@pytest.mark.asyncio
async def test_build_assignment_field_map_multiple_github_repos_single_field():
    cfg = _sample_cfg()
    cfg.plaky_field_repo = "fld_repo"
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        github_repos=["Org/A", "org/b", "Org/A"],
    )
    assert m.get("fld_repo") == "Org/A, org/b"


@pytest.mark.asyncio
async def test_build_assignment_field_map_repo_and_github_repos_keys():
    cfg = _sample_cfg()
    cfg.plaky_field_repo = "primary"
    cfg.plaky_field_github_repos = "all_repos"
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        github_repos=["deepiri-org/a", "deepiri-org/b"],
    )
    assert m.get("primary") == "deepiri-org/a"
    assert m.get("all_repos") == "deepiri-org/a, deepiri-org/b"


@pytest.mark.asyncio
async def test_build_assignment_field_map_override_wins():
    cfg = _sample_cfg()
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        field_overrides={"fld_qa": "manual-qa-id"},
    )
    assert m.get("fld_qa") == "manual-qa-id"


@pytest.mark.asyncio
async def test_assignment_preview_tool():
    from boardman.agent.tools.assignment_tools import _assignment_preview

    cfg = _sample_cfg()
    import boardman.assignment.qa_picker as qp

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qp, "load_team_assignments", lambda: cfg)
    try:
        raw = await _assignment_preview("deepiri-org/emotion-desktop")
        assert "qa-heavy" in raw
        assert "dev-1" in raw
    finally:
        monkeypatch.undo()
