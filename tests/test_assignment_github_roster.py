"""GitHub support-team roster merged into team_assignments (no static member list)."""

from __future__ import annotations

import json
import textwrap

import pytest
import yaml

from boardman.assignment import config
from boardman.plaky.client import PlakyClient


def test_github_roster_merge(tmp_path, monkeypatch):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"]},
                "member_overrides": {"alice": {"id": "plaky-alice"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()
    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {
            "ok": True,
            "members": [
                {"login": "alice", "name": "Alice"},
                {"login": "bob", "name": "Bob"},
            ],
        },
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].id == "plaky-alice"
    assert cfg.members[0].github_login == "alice"
    assert cfg.members[0].display == "Alice"
    assert "qa" in cfg.members[0].roles


def test_qa_excluded_merges_live_management_team_logins(tmp_path, monkeypatch):
    """qa_excluded_github_teams pulls in live team members as QA exclusions, in addition
    to the static qa_excluded list -- so a lead added to that GitHub team is excluded
    without a YAML/code change."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"]},
                "qa_excluded": ["Static Person"],
                "qa_excluded_github_teams": ["Team-Deepiri/it-management-team"],
                "members": [{"github_login": "alice", "id": "1"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()

    def _fake_roster(spec: str):
        if spec == "Team-Deepiri/it-management-team":
            return {"ok": True, "members": [{"login": "lead-login"}]}
        return {"ok": True, "members": []}

    monkeypatch.setattr("boardman.assignment.config.get_cached_support_team_roster", _fake_roster)
    cfg = config.load_team_assignments()
    assert "Static Person" in cfg.qa_excluded
    assert "lead-login" in cfg.qa_excluded


def test_qa_excluded_team_fetch_failure_keeps_static_list(tmp_path, monkeypatch):
    """A failed/unreachable team roster must not blow away the static exclusion list."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "qa_excluded": ["Static Person"],
                "qa_excluded_github_teams": ["Team-Deepiri/it-management-team"],
                "members": [{"github_login": "alice", "id": "1"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()
    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": False, "message": "boom"},
    )
    cfg = config.load_team_assignments()
    assert cfg.qa_excluded == ["Static Person"]


def test_live_qa_tier_team_overrides_yaml_qa_tier(tmp_path, monkeypatch):
    """A GitHub team named like `qa-tier-3` is live evidence of someone's real QA tier
    and must win over whatever number is hand-typed in member_defaults/member_overrides
    -- per Joe: never hardcode a person's tier when there's a live signal for it."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"], "qa_tier": 1},
                "member_overrides": {"alice": {"id": "plaky-alice"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "github_qa_tier_team_scan_enabled", True)
    monkeypatch.setattr(config.settings, "github_org", "Team-Deepiri")
    config._raw.cache_clear()
    config._qa_tier_teams_cache = None

    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alice", "name": "Alice"}]},
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    monkeypatch.setattr(config, "github_auth_available", lambda: True)
    monkeypatch.setattr(
        config,
        "fetch_login_max_qa_tier_from_org_teams_sync",
        lambda client, org, headers: ({"alice": 3}, ["qa-tier-3(t3)"]),
    )
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 3


def test_no_live_qa_tier_team_keeps_explicit_member_override(tmp_path, monkeypatch):
    """No matching org team is 'no evidence' -- but a per-member `qa_tier:` a human
    explicitly typed for THIS login still stands; it's only the blanket
    member_defaults.qa_tier guess that yields to the cold-start default."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"], "qa_tier": 1},
                "member_overrides": {"alice": {"id": "plaky-alice", "qa_tier": 2}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "github_qa_tier_team_scan_enabled", True)
    monkeypatch.setattr(config.settings, "github_org", "Team-Deepiri")
    config._raw.cache_clear()
    config._qa_tier_teams_cache = None

    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alice", "name": "Alice"}]},
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    monkeypatch.setattr(config, "github_auth_available", lambda: True)
    monkeypatch.setattr(
        config,
        "fetch_login_max_qa_tier_from_org_teams_sync",
        lambda client, org, headers: ({}, []),
    )
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 2
    assert cfg.members[0].qa_tier_is_explicit_override is True


def test_no_signal_at_all_falls_back_to_cold_start_default(tmp_path, monkeypatch):
    """No live team tier, no explicit per-member override, GitHunt off: a blanket
    member_defaults.qa_tier guess must NOT stand in as if it were a real decision --
    the person gets qa_tier_cold_start_default, never a silent 'assume tier 3'."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"], "qa_tier": 3},
                "member_overrides": {"alice": {"id": "plaky-alice"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "github_qa_tier_team_scan_enabled", True)
    monkeypatch.setattr(config.settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(config.settings, "qa_tier_cold_start_default", 2)
    monkeypatch.setattr(config.settings, "githunt_api_key", "")
    config._raw.cache_clear()
    config._qa_tier_teams_cache = None

    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alice", "name": "Alice"}]},
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    monkeypatch.setattr(config, "github_auth_available", lambda: True)
    monkeypatch.setattr(
        config,
        "fetch_login_max_qa_tier_from_org_teams_sync",
        lambda client, org, headers: ({}, []),
    )
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 2
    assert cfg.members[0].qa_tier_is_explicit_override is False


def test_githunt_cold_start_seeds_qa_tier_when_configured(tmp_path, monkeypatch):
    """With no live team tier and no explicit override, a configured GitHunt key seeds
    the starting qa_tier from the cached/looked-up activity+tech-stack score."""
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"]},
                "member_overrides": {"alice": {"id": "plaky-alice"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "github_qa_tier_team_scan_enabled", True)
    monkeypatch.setattr(config.settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(config.settings, "githunt_api_key", "test-key")
    config._raw.cache_clear()
    config._qa_tier_teams_cache = None

    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alice", "name": "Alice"}]},
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    monkeypatch.setattr(config, "github_auth_available", lambda: True)
    monkeypatch.setattr(
        config,
        "fetch_login_max_qa_tier_from_org_teams_sync",
        lambda client, org, headers: ({}, []),
    )

    from boardman.github import githunt_enrichment

    monkeypatch.setattr(githunt_enrichment, "cached_profile", lambda login: None)
    monkeypatch.setattr(githunt_enrichment, "has_cached", lambda login: False)
    monkeypatch.setattr(
        githunt_enrichment,
        "fetch_and_cache_profile_sync",
        lambda login: {"activity_score": 90, "tech_stack_score": 88},
    )

    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 3


def _base_cold_start_yml(tmp_path):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe", "qa": "fq"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["qa"]},
                "member_overrides": {"alice": {"id": "plaky-alice"}},
            }
        ),
        encoding="utf-8",
    )
    return yml


def _patch_cold_start_common(monkeypatch, tmp_path, yml):
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "github_qa_tier_team_scan_enabled", True)
    monkeypatch.setattr(config.settings, "github_org", "Team-Deepiri")
    monkeypatch.setattr(
        config.settings, "qa_capability_profiles_json_path", str(tmp_path / "profiles.json")
    )
    config._raw.cache_clear()
    config._qa_tier_teams_cache = None
    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {"ok": True, "members": [{"login": "alice", "name": "Alice"}]},
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {"ok": True, "users": []},
    )
    monkeypatch.setattr(config, "github_auth_available", lambda: True)
    monkeypatch.setattr(
        config,
        "fetch_login_max_qa_tier_from_org_teams_sync",
        lambda client, org, headers: ({}, []),
    )


def test_mined_capability_profile_seeds_qa_tier(tmp_path, monkeypatch):
    """Our own mined-commit-history profile (scripts/mine_qa_repo_capability.py's
    output) seeds the cold-start qa_tier when present, without needing GitHunt at all."""
    yml = _base_cold_start_yml(tmp_path)
    _patch_cold_start_common(monkeypatch, tmp_path, yml)
    monkeypatch.setattr(config.settings, "githunt_api_key", "")

    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps({"profiles": {"alice": {"qa_tier": 3, "repos_mined": 4}}}),
        encoding="utf-8",
    )

    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 3


def test_mined_capability_profile_wins_over_githunt(tmp_path, monkeypatch):
    """Our own demonstrated-activity evidence outranks GitHunt's opaque score --
    GitHunt is only consulted when we have no mined profile for this login."""
    yml = _base_cold_start_yml(tmp_path)
    _patch_cold_start_common(monkeypatch, tmp_path, yml)
    monkeypatch.setattr(config.settings, "githunt_api_key", "test-key")

    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        json.dumps({"profiles": {"alice": {"qa_tier": 1, "repos_mined": 2}}}),
        encoding="utf-8",
    )

    from boardman.github import githunt_enrichment

    def _fail(*args, **kwargs):
        raise AssertionError("GitHunt must not be consulted when a mined profile exists")

    monkeypatch.setattr(githunt_enrichment, "cached_profile", _fail)
    monkeypatch.setattr(githunt_enrichment, "has_cached", _fail)
    monkeypatch.setattr(githunt_enrichment, "fetch_and_cache_profile_sync", _fail)

    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 1


def test_missing_capability_profiles_file_falls_through_cleanly(tmp_path, monkeypatch):
    """No qa_capability_profiles.json at all (script never run yet) must not error --
    it's additive evidence, same contract as every other optional cache in this module."""
    yml = _base_cold_start_yml(tmp_path)
    _patch_cold_start_common(monkeypatch, tmp_path, yml)
    monkeypatch.setattr(config.settings, "githunt_api_key", "")
    monkeypatch.setattr(config.settings, "qa_tier_cold_start_default", 2)

    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].qa_tier == 2


def test_explicit_members_list_skips_github_fetch(tmp_path, monkeypatch):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        textwrap.dedent("""
            plaky_field_keys:
              engineer: fe
              qa: fq
            members:
              - id: static-only
                roles: [engineer]
                repo_globs: ["deepiri-org/*"]
            """).strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()

    def _fail(_spec):
        raise AssertionError("GitHub roster should not load when explicit members are set")

    monkeypatch.setattr("boardman.assignment.config.get_cached_support_team_roster", _fail)
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].id == "static-only"


def test_use_github_false_skips_fetch(tmp_path, monkeypatch):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe"},
                "use_github_support_team_roster": False,
                "member_overrides": {"alice": {"id": "x"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()

    def _fail(_spec):
        raise AssertionError("GitHub disabled")

    monkeypatch.setattr("boardman.assignment.config.get_cached_support_team_roster", _fail)
    cfg = config.load_team_assignments()
    assert cfg.members == []


def test_auto_match_plaky_without_explicit_id(tmp_path, monkeypatch):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {"engineer": "fe"},
                "member_defaults": {"repo_globs": ["deepiri-org/*"], "roles": ["engineer"]},
                "member_overrides": {"alice": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()
    monkeypatch.setattr(
        "boardman.assignment.config.get_cached_support_team_roster",
        lambda spec: {
            "ok": True,
            "members": [{"login": "alice", "name": "Alice A", "email": "alice@work.com"}],
        },
    )
    monkeypatch.setattr(
        PlakyClient,
        "list_workspace_users_sync",
        lambda self: {
            "ok": True,
            "users": [
                {
                    "id": "plaky-from-match",
                    "name": "Alice A",
                    "email": "alice@other.com",
                }
            ],
        },
    )
    cfg = config.load_team_assignments()
    assert len(cfg.members) == 1
    assert cfg.members[0].id == "plaky-from-match"
    assert cfg.members[0].github_login == "alice"


def test_infer_plaky_field_keys_from_normalized():
    inferred = config.infer_plaky_field_keys_from_normalized(
        {
            "fields": [
                {"key": "dev_person", "name": "Engineer", "type": "PERSON"},
                {"key": "qa_person", "name": "QA Engineer", "type": "PERSON"},
                {"key": "gh_repo", "name": "GitHub Repo", "type": "TEXT"},
                {"key": "gh_repos", "name": "GitHub Repos", "type": "TEXT"},
            ]
        }
    )
    assert inferred == {
        "engineer": "dev_person",
        "qa": "qa_person",
        "repo": "gh_repo",
        "github_repos": "gh_repos",
    }


def test_infer_plaky_field_keys_when_plaky_omits_field_type():
    """Plaky often returns columns with no usable `type` — name heuristics must still infer keys."""
    inferred = config.infer_plaky_field_keys_from_normalized(
        {
            "fields": [
                {"key": "k_contrib", "name": "Contributor", "type": ""},
                {"key": "k_qa", "label": "QA", "type": ""},
                {"key": "k_repo", "title": "GitHub repository", "type": ""},
            ]
        }
    )
    assert inferred.get("engineer") == "k_contrib"
    assert inferred.get("qa") == "k_qa"
    assert inferred.get("repo") == "k_repo"


def test_infer_plaky_field_keys_does_not_collide_engineer_and_qa():
    """Two PERSON columns with no name matching "qa"/"engineer"/etc must resolve to
    DIFFERENT keys. Colliding here means the QA-assignment write (which runs after
    the engineer/assignee write on PR-open) silently overwrites the assignee column
    with the QA reviewer, per the deepiri-cascade#70 report (Joel assignee -> Sergio
    reviewer request -> Plaky showed Sergio as the assignee)."""
    inferred = config.infer_plaky_field_keys_from_normalized(
        {
            "fields": [
                {"key": "person-1", "name": "Assignee", "type": "PERSON"},
                {"key": "person-2", "name": "Reviewer", "type": "PERSON"},
            ]
        }
    )
    assert inferred.get("engineer") != inferred.get("qa")


def test_infer_plaky_field_keys_single_person_field_leaves_qa_unresolved():
    """Only one PERSON column on the board: engineer claims it, qa must NOT collide."""
    inferred = config.infer_plaky_field_keys_from_normalized(
        {"fields": [{"key": "person-1", "name": "Owner", "type": "PERSON"}]}
    )
    assert inferred.get("engineer") == "person-1"
    assert "qa" not in inferred


@pytest.mark.asyncio
async def test_sync_team_assignment_field_keys_from_board_updates_only_blanks(
    tmp_path, monkeypatch
):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump(
            {
                "plaky_field_keys": {
                    "engineer": "",
                    "qa": "manual-qa",
                    "repo": "",
                    "github_repos": "",
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    config._raw.cache_clear()

    async def fake_fetch(_board_id: str):
        return {
            "ok": True,
            "normalized": {
                "fields": [
                    {"key": "eng_key", "name": "Engineer", "type": "PERSON"},
                    {"key": "qa_key", "name": "QA", "type": "PERSON"},
                    {"key": "repo_key", "name": "GitHub Repo", "type": "TEXT"},
                    {"key": "repos_key", "name": "GitHub Repos", "type": "TEXT"},
                ]
            },
        }

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", fake_fetch)
    res = await config.sync_team_assignment_field_keys_from_board("board-1")
    data = yaml.safe_load(yml.read_text(encoding="utf-8"))

    assert res["ok"] is True
    assert data["plaky_field_keys"] == {
        "engineer": "eng_key",
        "qa": "manual-qa",
        "repo": "repo_key",
        "github_repos": "repos_key",
    }


@pytest.mark.asyncio
async def test_sync_team_assignment_field_keys_from_board_respects_cooldown(tmp_path, monkeypatch):
    yml = tmp_path / "ta.yml"
    yml.write_text(
        yaml.dump({"plaky_field_keys": {"repo": "", "github_repos": ""}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(config.settings, "team_assignments_yml_path", str(yml))
    monkeypatch.setattr(config.settings, "plaky_team_assignment_field_sync_cooldown_seconds", 60.0)
    config._raw.cache_clear()
    config._last_field_sync_by_board.clear()

    calls = {"n": 0}

    async def fake_fetch(_board_id: str):
        calls["n"] += 1
        return {
            "ok": True,
            "normalized": {"fields": [{"key": "repo_key", "name": "GitHub Repo", "type": "TEXT"}]},
        }

    monkeypatch.setattr("boardman.plaky.board_schema.fetch_board_schema_bundle", fake_fetch)
    first = await config.sync_team_assignment_field_keys_from_board("board-cooldown")
    second = await config.sync_team_assignment_field_keys_from_board("board-cooldown")
    assert first["ok"] is True
    assert second["ok"] is True
    assert second.get("skipped") is True
    assert "cooldown" in (second.get("message") or "").lower()
    assert calls["n"] == 1
