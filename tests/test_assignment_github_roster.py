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
        textwrap.dedent(
            """
            plaky_field_keys:
              engineer: fe
              qa: fq
            members:
              - id: static-only
                roles: [engineer]
                repo_globs: ["deepiri-org/*"]
            """
        ).strip(),
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
