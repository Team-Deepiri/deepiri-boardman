"""Board-aware placement + person-field resolution (2026-06 category-board redesign)."""

from __future__ import annotations

import pytest

import boardman.plaky.board_aware as ba
from boardman.assignment.qa_picker import build_assignment_field_map
from boardman.plaky.board_aware import (
    board_person_field_keys,
    clear_group_cache,
    resolve_group_for_repo,
)

# Schemas mirroring the live category boards (keys intentionally differ per board).
PLATFORM_NORMALIZED = {
    "board_name": "Deepiri Platform + Services",
    "fields": [
        {"key": "person-2", "name": "Assignee", "type": "PERSON"},
        {"key": "status-3", "name": "Type", "type": "STATUS"},
        {"key": "status-4", "name": "Status", "type": "STATUS"},
        {"key": "status-5", "name": "Priority", "type": "STATUS"},
        {"key": "person-3", "name": "QA Engineer Assigned", "type": "PERSON"},
    ],
    "groups": [],
}
DEVTOOLS_NORMALIZED = {
    "board_name": "Developer Tool Repos",
    "fields": [
        {"key": "person-4", "name": "Assignee", "type": "PERSON"},
        {"key": "person-5", "name": "QA Engineer Assigned", "type": "PERSON"},
    ],
    "groups": [],
}


@pytest.fixture(autouse=True)
def _fresh_group_cache():
    clear_group_cache()
    yield
    clear_group_cache()


class FakePlaky:
    def __init__(self, groups_by_board):
        self.groups_by_board = groups_by_board
        self.calls = 0

    async def list_groups(self, board_id):
        self.calls += 1
        groups = self.groups_by_board.get(str(board_id))
        if groups is None:
            return {"ok": False, "groups": [], "message": "not found"}
        return {"ok": True, "groups": groups}


@pytest.mark.asyncio
async def test_resolve_group_matches_repo_name_case_insensitive():
    plaky = FakePlaky(
        {
            "269014": [
                {"id": 927117, "name": "deepiri-api-gateway"},
                {"id": 907428, "name": "deepiri-platform"},
            ]
        }
    )
    gid = await resolve_group_for_repo("269014", "Deepiri-API-Gateway", "999", plaky=plaky)
    assert gid == "927117"


@pytest.mark.asyncio
async def test_resolve_group_falls_back_when_no_repo_group():
    plaky = FakePlaky({"269029": [{"id": 907467, "name": "Open PRs"}]})
    gid = await resolve_group_for_repo("269029", "deepiri-boardman", "907467", plaky=plaky)
    assert gid == "907467"


@pytest.mark.asyncio
async def test_resolve_group_fallback_on_api_failure():
    plaky = FakePlaky({})  # every board returns ok=False
    gid = await resolve_group_for_repo("269028", "deepiri-sorge", "fallback-gid", plaky=plaky)
    assert gid == "fallback-gid"


@pytest.mark.asyncio
async def test_resolve_group_uses_cache_within_ttl():
    plaky = FakePlaky({"269014": [{"id": 927117, "name": "deepiri-api-gateway"}]})
    first = await resolve_group_for_repo("269014", "deepiri-api-gateway", None, plaky=plaky)
    second = await resolve_group_for_repo("269014", "deepiri-api-gateway", None, plaky=plaky)
    assert first == second == "927117"
    assert plaky.calls == 1


@pytest.mark.asyncio
async def test_board_person_field_keys_platform_board(monkeypatch):
    async def fake_bundle(board_id):
        return {"ok": True, "normalized": PLATFORM_NORMALIZED}

    monkeypatch.setattr(ba, "fetch_board_schema_bundle", fake_bundle)
    keys = await board_person_field_keys("269014")
    # On the Platform board the global config keys (person-3/person-4) would target
    # the QA column / a nonexistent field; schema says Assignee=person-2, QA=person-3.
    assert keys == {"engineer": "person-2", "qa": "person-3"}


@pytest.mark.asyncio
async def test_board_person_field_keys_devtools_board(monkeypatch):
    async def fake_bundle(board_id):
        return {"ok": True, "normalized": DEVTOOLS_NORMALIZED}

    monkeypatch.setattr(ba, "fetch_board_schema_bundle", fake_bundle)
    keys = await board_person_field_keys("269029")
    assert keys == {"engineer": "person-4", "qa": "person-5"}


@pytest.mark.asyncio
async def test_board_person_field_keys_none_when_schema_unavailable(monkeypatch):
    async def fake_bundle(board_id):
        return {"ok": False, "normalized": None}

    monkeypatch.setattr(ba, "fetch_board_schema_bundle", fake_bundle)
    assert await board_person_field_keys("269014") is None
    assert await board_person_field_keys("") is None


@pytest.mark.asyncio
async def test_build_assignment_field_map_empty_override_disables_qa(monkeypatch):
    """Empty-string override (board schema known, no QA column) must NOT fall back to
    the global key — that would write the QA id into the wrong column."""
    from boardman.assignment.config import TeamAssignmentsConfig, TeamMember

    cfg = TeamAssignmentsConfig(
        plaky_field_engineer="person-3",
        plaky_field_qa="person-4",
        members=[
            TeamMember(
                id="plaky-qa-1",
                display="QA One",
                roles=["qa"],
                qa_tier=3,
                tier="standard",
                repo_globs=["*"],
                weight=1.0,
            )
        ],
        random_jitter=0.0,
    )
    m = await build_assignment_field_map("Team-Deepiri/deepiri-platform", cfg, plaky_field_qa_key="")
    assert "person-4" not in m  # global key must not leak through

    m2 = await build_assignment_field_map("Team-Deepiri/deepiri-platform", cfg, plaky_field_qa_key=None)
    assert m2.get("person-4") == "plaky-qa-1"  # None keeps legacy behavior
