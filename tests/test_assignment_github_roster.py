"""GitHub support-team roster merged into team_assignments (no static member list)."""

from __future__ import annotations

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
