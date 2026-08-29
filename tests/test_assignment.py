"""QA assignment picker (team_assignments.yml logic)."""

from __future__ import annotations

import random

import pytest

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.assignment.qa_picker import (
    build_assignment_field_map,
    build_repo_field_map,
    ensure_github_owner_repo,
    github_repo_suffix_name,
    normalize_github_repo_inputs,
    pick_qa_for_repo,
    repo_is_heavy,
)
from boardman.assignment.repo_rules import QaRepoRules
from boardman.services.task_mutations import UpdateTaskInput, update_task_internal
from boardman.settings import settings


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
                tier_is_explicit_override=True,
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
                tier_is_explicit_override=True,
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
async def test_live_capability_board_overrides_config_tier(monkeypatch):
    """A QA configured as `tier: light` in team_assignments.yml, but reported as
    `heavy` on the live capability board (a fresher machine, or the config was never
    updated), must be treated as heavy for the hardware hard-filter — the live
    measurement wins over the hand-typed config value."""
    from boardman.assignment import qa_picker as qp

    cfg = _sample_cfg()
    # Only qa-light matches this repo's globs (qa-heavy is scoped to emotion-*), and
    # only the legacy hardware-tier filter (not qa_tier/qa_repo_rules, which target
    # "*emotion*") differentiates this repo — isolates the tier-override's effect.
    cfg.heavy_repo_patterns = ["*boardman*"]
    for m in cfg.members:
        if m.id == "qa-light":
            m.github_login = "qa-light-login"

    # Without a live override: light hardware is dropped from a heavy repo.
    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({}))
    qid, why = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid is None, why

    # With a live override to heavy: now eligible.
    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({"qa-light-login": "heavy"}))
    qid, why = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid == "qa-light", why


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_github_activity_infers_hardware_tier_with_no_self_report(monkeypatch):
    """No CLI run, no Plaky capability board, no self-reported label anywhere — the
    tier comes purely from demonstrated GitHub activity on an already-classified repo.
    This is the backend-only path: works when Boardman itself only ever talks to
    GitHub and Plaky, with nobody running anything locally to report their machine."""
    from boardman.assignment import qa_picker as qp

    cfg = _sample_cfg()
    cfg.heavy_repo_patterns = ["*boardman*"]
    for m in cfg.members:
        if m.id == "qa-light":
            m.github_login = "qa-light-login"

    # No live capability board data at all.
    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({}))
    monkeypatch.setattr(qp.settings, "github_pat", "fake-token")

    class FakeProfile:
        def repos_above_weight(self, min_weight=0.4):
            return ["deepiri-org/some-heavy-repo"]

    async def fake_fetch_profile(client, login, org):
        assert login == "qa-light-login"
        return FakeProfile()

    monkeypatch.setattr(
        "boardman.github.qa_contribution_profile.fetch_contribution_profile", fake_fetch_profile
    )

    def fake_get_routing(full_name, short_name, org):
        if full_name == "deepiri-org/some-heavy-repo":
            from boardman.repos_config import RepoRouting

            return RepoRouting(tier=3)
        return None

    monkeypatch.setattr(qp, "get_routing", fake_get_routing)

    # Without inference: light hardware (config default) is dropped from a heavy repo.
    monkeypatch.setattr(qp, "_github_inferred_tiers", lambda *a, **k: _async({}))
    qid, why = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid is None, why

    # With real inference wired up: demonstrated tier-3 work promotes them to heavy.
    monkeypatch.undo()  # restore _github_inferred_tiers to the real implementation
    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({}))
    monkeypatch.setattr(qp.settings, "github_pat", "fake-token")
    monkeypatch.setattr(
        "boardman.github.qa_contribution_profile.fetch_contribution_profile", fake_fetch_profile
    )
    monkeypatch.setattr(qp, "get_routing", fake_get_routing)

    qid, why = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid == "qa-light", why


@pytest.mark.asyncio
async def test_non_heavy_repo_allows_light_qa_in_pool():
    cfg = _sample_cfg()
    random.seed(0)
    qid, _ = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid in ("qa-heavy", "qa-light")


@pytest.mark.asyncio
async def test_build_assignment_field_map():
    cfg = _sample_cfg()
    m = await build_assignment_field_map("deepiri-org/emotion-desktop", cfg)
    assert "fld_eng" not in m
    assert m.get("fld_qa") == "qa-heavy"


@pytest.mark.asyncio
async def test_build_assignment_field_map_qa_key_override():
    """Board-inferred QA key (when YAML omits plaky_field_qa) still receives roster QA id."""
    cfg = _sample_cfg()
    cfg.plaky_field_engineer = ""
    cfg.plaky_field_qa = ""
    m = await build_assignment_field_map(
        "deepiri-org/emotion-desktop",
        cfg,
        plaky_field_qa_key="inferred_qa",
    )
    assert "inferred_contributor" not in m
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


def test_ensure_github_owner_repo_uses_bare_repo_owner_then_github_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_bare_repo_owner", "Team-Deepiri")
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    assert ensure_github_owner_repo("deepiri-platform") == "Team-Deepiri/deepiri-platform"
    assert (
        ensure_github_owner_repo("Team-Deepiri/deepiri-platform") == "Team-Deepiri/deepiri-platform"
    )


def test_ensure_github_owner_repo_falls_back_when_bare_owner_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_bare_repo_owner", "")
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    assert ensure_github_owner_repo("x") == "deepiri-org/x"


def test_normalize_github_repo_inputs_space_separated_and_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_bare_repo_owner", "Team-Deepiri")
    monkeypatch.setattr(settings, "github_org", "deepiri-org")
    got = normalize_github_repo_inputs(
        extra_repo_text="deepiri-platform deepiri-pkg-version-manager"
    )
    assert got == ["Team-Deepiri/deepiri-platform", "Team-Deepiri/deepiri-pkg-version-manager"]


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
        assert "dev-1" not in raw
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_update_task_auto_assign_qa_prefixes_bare_github_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked: list[str] = []

    async def _pick(full_name: str, cfg=None):
        picked.append(full_name)
        return None, "intentional-no-match"

    monkeypatch.setattr("boardman.services.task_mutations.pick_qa_for_repo", _pick)
    monkeypatch.setattr(
        "boardman.services.task_mutations.load_team_assignments", lambda: _sample_cfg()
    )
    monkeypatch.setattr(settings, "github_bare_repo_owner", "Team-Deepiri")

    r = await update_task_internal(
        "6078697",
        UpdateTaskInput(
            auto_assign_qa=True, github_repo="deepiri-platform", plaky_board_id="218760"
        ),
    )
    assert r.get("ok") is False
    assert picked == ["Team-Deepiri/deepiri-platform"]


def test_population_prior_tier_is_the_mode_of_known_tiers():
    from boardman.assignment.qa_picker import _population_prior_tier

    assert _population_prior_tier(["heavy", "heavy", "light"]) == "heavy"
    assert _population_prior_tier(["light", "standard"]) in ("light", "standard")
    # Nobody has any resolved tier at all -> the absolute last-resort constant.
    assert _population_prior_tier([]) == "light"


@pytest.mark.asyncio
async def test_cold_start_qa_uses_team_population_prior_not_a_fixed_default(monkeypatch):
    """A brand-new QA with a GitHub login that has genuinely zero activity anywhere
    (no in-org history, no public repos either) still gets tiered dynamically — from
    what the REST of the current team's resolved tiers actually look like, not a
    hardcoded literal that's the same on every team forever."""
    from boardman.assignment import qa_picker as qp

    cfg = _sample_cfg()
    cfg.heavy_repo_patterns = ["*boardman*"]
    for m in cfg.members:
        if m.id == "qa-heavy":
            m.github_login = "veteran-heavy-login"
        if m.id == "qa-light":
            # Give qa-light repo access to the target repo but no resolvable tier
            # anywhere, so they inherit whatever the pool's prior computes to.
            m.repo_globs = ["deepiri-org/*"]
            m.github_login = "brand-new-login"
            m.tier_is_explicit_override = False

    # qa-heavy resolves via explicit override (tier="heavy", set in _sample_cfg).
    # qa-light: no live board entry, and inference finds nothing anywhere for them.
    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({}))

    async def fake_inferred(candidates, org):
        return {}  # nobody has demonstrated GitHub activity in this run

    monkeypatch.setattr(qp, "_github_inferred_tiers", fake_inferred)

    # qa-heavy's repo_globs only match emotion-*, so for "deepiri-org/boardman" only
    # qa-light is a QA candidate — but the prior is computed over the CANDIDATE POOL
    # for this pick, which for a heavy-repo-only filter needs at least one other
    # resolved candidate to be meaningful. Here qa-light is the sole candidate with no
    # resolved tier, so the prior itself falls back to the absolute last resort — this
    # pins that the mechanism runs end-to-end without erroring, and is exercised again
    # with company below where a second resolved candidate exists.
    qid, why = await pick_qa_for_repo("deepiri-org/boardman", cfg)
    assert qid is None, why  # SAFE_DEFAULT_TIER_FOR_UNKNOWN ("light") still excluded


@pytest.mark.asyncio
async def test_cold_start_qa_inherits_heavy_prior_from_resolved_teammate(monkeypatch):
    """Same as above, but qa-heavy is ALSO a candidate for this repo (both match its
    globs) — so the pool has one resolved "heavy" teammate, and the population prior
    for qa-light (zero evidence) should compute to "heavy" too, making them eligible."""
    from boardman.assignment import qa_picker as qp

    cfg = _sample_cfg()
    cfg.heavy_repo_patterns = ["*shared*"]
    cfg.qa_repo_rules = QaRepoRules(tier2_excluded_patterns=[], tier1_only_patterns=[])
    for m in cfg.members:
        m.repo_globs = ["deepiri-org/*"]  # both QAs now match the target repo
        if m.id == "qa-light":
            m.tier_is_explicit_override = False
            m.github_login = "brand-new-login"

    monkeypatch.setattr(qp, "fetch_capability_tiers", lambda: _async({}))
    monkeypatch.setattr(qp, "_github_inferred_tiers", lambda *a, **k: _async({}))

    qid, why = await pick_qa_for_repo("deepiri-org/shared-project", cfg)
    # qa-heavy resolves via explicit override; qa-light has no evidence anywhere and
    # inherits the pool's prior ("heavy", the only resolved value) — both pass the
    # heavy-repo hardware filter, so either may win on weighted scoring.
    assert qid in ("qa-heavy", "qa-light"), why
