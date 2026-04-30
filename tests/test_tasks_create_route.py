"""POST /api/v1/tasks — placement, field map, and post-create PATCH wiring."""

from __future__ import annotations

from typing import Any, Dict

import pytest
from httpx import ASGITransport, AsyncClient

from boardman.assignment.config import TeamAssignmentsConfig, TeamMember, TierSpec
from boardman.main import create_app


def _cfg_placeholder_yaml_keys() -> TeamAssignmentsConfig:
    """Mimics a mis-copied example YAML using template keys (not real Plaky itemFieldKeys)."""
    return TeamAssignmentsConfig(
        plaky_field_engineer="person-1",
        plaky_field_qa="person-2",
        plaky_field_repo="fld_repo",
        plaky_field_github_repos="fld_repos_multi",
        tiers={"standard": TierSpec("standard", 1.0)},
        members=[
            TeamMember(
                id="qa-1",
                display="QA",
                roles=["qa"],
                tier="standard",
                qa_tier=2,
                repo_globs=["acme/*"],
                weight=1.0,
            ),
            TeamMember(
                id="eng-1",
                display="Eng",
                roles=["engineer"],
                repo_globs=["acme/*"],
                weight=1.0,
            ),
        ],
        heavy_repo_patterns=[],
        random_jitter=0.0,
    )


def _cfg_native_plaky_yaml_keys() -> TeamAssignmentsConfig:
    """YAML keys match native Plaky board field ids (person-1, tag-2, …)."""
    return TeamAssignmentsConfig(
        plaky_field_engineer="person-1",
        plaky_field_qa="person-2",
        plaky_field_repo="tag-2",
        plaky_field_github_repos="tag-2",
        tiers={"standard": TierSpec("standard", 1.0)},
        members=[
            TeamMember(
                id="qa-1",
                display="QA",
                roles=["qa"],
                tier="standard",
                qa_tier=2,
                repo_globs=["acme/*"],
                weight=1.0,
            ),
            TeamMember(
                id="eng-1",
                display="Eng",
                roles=["engineer"],
                repo_globs=["acme/*"],
                weight=1.0,
            ),
        ],
        heavy_repo_patterns=[],
        random_jitter=0.0,
    )


def _cfg_for_route() -> TeamAssignmentsConfig:
    return TeamAssignmentsConfig(
        plaky_field_engineer="fld_eng",
        plaky_field_qa="fld_qa",
        plaky_field_repo="fld_repo",
        plaky_field_github_repos="fld_repos_multi",
        tiers={"standard": TierSpec("standard", 1.0)},
        members=[
            TeamMember(
                id="qa-1",
                display="QA",
                roles=["qa"],
                tier="standard",
                qa_tier=2,
                repo_globs=["acme/*"],
                weight=1.0,
            ),
            TeamMember(
                id="eng-1",
                display="Eng",
                roles=["engineer"],
                repo_globs=["acme/*"],
                weight=1.0,
            ),
        ],
        heavy_repo_patterns=[],
        random_jitter=0.0,
    )


@pytest.mark.asyncio
async def test_post_tasks_passes_board_to_plaky_and_patches_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _empty_schema_bundle(_board_id: str):
        return {"normalized": {"fields": []}}

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-from-api"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create_kwargs"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-42"}, "task_id": "item-42"}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch"] = (board_id, item_id, dict(values))
            return {"ok": True, "mode": "bulk", "patched_keys": list(values)}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id, "refetched": True}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _empty_schema_bundle)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_for_route())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Route test task",
                "description": "",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    post = body.get("post_create_assignment") or {}
    assert post.get("ok") is True, post
    assert (post.get("board_item") or {}).get("refetched") is True

    ck = captured.get("create_kwargs") or {}
    assert ck.get("board_id") == "board-77"
    assert ck.get("group_id") == "grp-1"
    assert ck.get("defer_field_patch") is False

    patch = captured.get("patch")
    assert patch is not None
    board_id, item_id, values = patch
    assert board_id == "board-77"
    assert item_id == "item-42"
    assert values.get("fld_eng") == "eng-1"
    assert values.get("fld_qa") == "qa-1"
    assert values.get("fld_repo") == "acme/widget"


@pytest.mark.asyncio
async def test_post_tasks_scrubs_placeholder_yaml_keys_and_infers_real_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _schema_with_person_columns(_board_id: str):
        return {
            "normalized": {
                "fields": [
                    {"name": "Contributor", "key": "col_contributor", "type": "PERSON"},
                    {"name": "QA engineer assigned", "key": "col_qa_person", "type": "PERSON"},
                ]
            }
        }

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-from-api"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create_kwargs"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-42"}, "task_id": "item-42"}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch"] = (board_id, item_id, dict(values))
            return {"ok": True, "mode": "bulk", "patched_keys": list(values)}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_with_person_columns)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_placeholder_yaml_keys())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Scrub keys",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body

    patch = captured.get("patch")
    assert patch is not None
    _, _, values = patch
    assert "person-1" not in values
    assert values.get("col_contributor") == "eng-1"
    assert values.get("col_qa_person") == "qa-1"


@pytest.mark.asyncio
async def test_post_tasks_keeps_native_plaky_keys_when_they_appear_on_board_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plaky uses ids like person-1 / status-1; YAML may legitimately match those keys."""
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _schema_native_keys(_board_id: str):
        return {
            "normalized": {
                "fields": [
                    {"name": "Contributor", "key": "person-1", "type": "PERSON"},
                    {"name": "QA", "key": "person-2", "type": "PERSON"},
                    {"name": "GitHub Repos", "key": "tag-2", "type": "TAG"},
                ]
            }
        }

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-1"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create_kwargs"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-42"}, "task_id": "item-42"}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch"] = (board_id, item_id, dict(values))
            return {"ok": True, "mode": "bulk", "patched_keys": list(values)}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_native_keys)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_native_plaky_yaml_keys())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Native keys",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    patch = captured.get("patch")
    assert patch is not None
    _, _, values = patch
    assert values.get("person-1") == "eng-1"
    assert values.get("person-2") == "qa-1"
    assert values.get("tag-2") == "widget"


@pytest.mark.asyncio
async def test_post_tasks_merges_default_status_type_priority_from_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _schema_bundle(_board_id: str):
        return {
            "normalized": {
                "fields": [
                    {
                        "name": "Status",
                        "key": "fld_status",
                        "type": "SELECT",
                        "options": [
                            {"name": "To Do", "id": 10},
                            {"name": "In Progress", "id": 20},
                        ],
                    },
                    {
                        "name": "Type",
                        "key": "fld_type",
                        "type": "SELECT",
                        "options": [{"name": "Bug", "id": 1}, {"name": "Feature", "id": 2}],
                    },
                    {
                        "name": "Priority",
                        "key": "fld_pri",
                        "type": "SELECT",
                        "options": [{"name": "Low", "id": 1}, {"name": "Medium", "id": 3}],
                    },
                ]
            }
        }

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-from-api"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create_kwargs"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-42"}, "task_id": "item-42"}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch"] = (board_id, item_id, dict(values))
            return {"ok": True, "mode": "bulk", "patched_keys": list(values)}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id, "refetched": True}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_bundle)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_for_route())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Schema defaults",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    patch = captured.get("patch")
    assert patch is not None
    _, _, values = patch
    assert values.get("fld_status") == 20
    assert values.get("fld_type") == 2
    assert values.get("fld_pri") == 3

    ck = captured.get("create_kwargs") or {}
    assert ck.get("field_values") is not None
    assert ck["field_values"].get("fld_status") == 20
    assert ck.get("defer_field_patch") is False
    assert ck.get("priority") == "medium"


@pytest.mark.asyncio
async def test_post_tasks_accepts_status_type_priority_tags_and_type_json_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _schema_bundle(_board_id: str):
        return {
            "normalized": {
                "fields": [
                    {
                        "name": "Status",
                        "key": "fld_status",
                        "type": "SELECT",
                        "options": [
                            {"name": "In Progress", "id": 20},
                            {"name": "Needs QA", "id": 30},
                        ],
                    },
                    {
                        "name": "Type",
                        "key": "fld_type",
                        "type": "SELECT",
                        "options": [{"name": "Bug", "id": 1}, {"name": "Feature", "id": 2}],
                    },
                    {
                        "name": "Priority",
                        "key": "fld_pri",
                        "type": "SELECT",
                        "options": [
                            {"name": "Medium", "id": 3},
                            {"name": "Very Important", "id": 9},
                        ],
                    },
                ]
            }
        }

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-from-api"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create_kwargs"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-99"}, "task_id": "item-99"}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch"] = (board_id, item_id, dict(values))
            return {"ok": True, "mode": "bulk", "patched_keys": list(values)}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id, "refetched": True}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_bundle)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_for_route())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Tagged task",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
                "status": "Needs QA",
                "type": "Bug",
                "priority": "Very Important",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    patch = captured.get("patch")
    assert patch is not None
    _, _, values = patch
    assert values.get("fld_status") == 30
    assert values.get("fld_type") == 1
    assert values.get("fld_pri") == 9
    ck = captured.get("create_kwargs") or {}
    assert ck.get("priority") == "high"


def test_field_stubs_from_board_items_extracts_item_field_keys():
    from boardman.plaky.board_schema import field_row_item_key, field_stubs_from_board_items

    stubs = field_stubs_from_board_items(
        [
            {
                "id": "it1",
                "itemFields": [
                    {"itemFieldKey": "fk_contrib", "name": "Contributor", "type": "USER"},
                    {"itemFieldKey": "fk_repo", "name": "GitHub repo"},
                ],
            }
        ]
    )
    keys = {field_row_item_key(s) for s in stubs}
    assert "fk_contrib" in keys
    assert "fk_repo" in keys


def test_select_field_patch_pair_from_schema_matches_option_id():
    from boardman.plaky.board_schema import select_field_patch_pair_from_schema

    norm = {
        "fields": [
            {
                "name": "Priority",
                "key": "pkey",
                "options": [{"name": "Medium", "id": 99}],
            }
        ]
    }
    pair = select_field_patch_pair_from_schema(
        norm,
        column_name_substrings=("priority",),
        value_label_candidates=("medium", "med"),
    )
    assert pair == ("pkey", 99)


@pytest.mark.asyncio
async def test_post_tasks_uses_board_from_create_when_patch_board_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _empty_schema_bundle(_board_id: str):
        return {"normalized": {"fields": []}}

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "g1"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            return {
                "ok": True,
                "task": {"id": "item-99", "boardId": "board-from-plaky"},
                "task_id": "item-99",
            }

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured["patch_board"] = board_id
            return {"ok": True, "mode": "bulk"}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _empty_schema_bundle)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_for_route())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "No board in json",
                "repo": "acme/widget",
                "auto_assign_team": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    assert captured.get("patch_board") == "board-from-plaky"


@pytest.mark.asyncio
async def test_patch_tasks_create_then_update_and_comment_field_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    async def _noop_sync(_board_id: str):
        return {"ok": True}

    async def _schema_bundle(_board_id: str):
        return {
            "normalized": {
                "fields": [
                    {"name": "Contributor", "key": "fld_eng", "type": "PERSON"},
                    {"name": "QA engineer assigned", "key": "fld_qa", "type": "PERSON"},
                    {"name": "Repository", "key": "fld_repo", "type": "TAG"},
                    {"name": "GitHub Repos", "key": "fld_repos_multi", "type": "TAG"},
                    {
                        "name": "Status",
                        "key": "fld_status",
                        "type": "SELECT",
                        "options": [{"name": "In Progress", "id": 20}, {"name": "Needs QA", "id": 30}],
                    },
                    {
                        "name": "Type",
                        "key": "fld_type",
                        "type": "SELECT",
                        "options": [{"name": "Feature", "id": 2}, {"name": "Bug", "id": 1}],
                    },
                    {
                        "name": "Priority",
                        "key": "fld_pri",
                        "type": "SELECT",
                        "options": [{"name": "Medium", "id": 3}, {"name": "Very Important", "id": 9}],
                    },
                ]
            }
        }

    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def list_groups(self, board_id: str) -> dict:
            return {"ok": True, "groups": [{"id": "grp-1"}]}

        async def create_task(self, **kwargs: Any) -> dict:
            captured["create"] = dict(kwargs)
            return {"ok": True, "task": {"id": "item-123"}, "task_id": "item-123"}

        async def get_board_item_public(self, board_id: str, item_id: str) -> dict:
            return {"ok": True, "item": {"id": item_id}}

        async def list_board_items(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "items": []}

        async def update_task_fields(self, task_id: str, **kwargs: Any) -> dict:
            captured["title"] = {"task_id": task_id, **kwargs}
            return {"ok": True, "task": {"id": task_id}}

        async def add_comment(self, task_id: str, body: str) -> dict:
            captured["comment"] = {"task_id": task_id, "body": body}
            return {"ok": True, "comment": {"id": "c1"}}

        async def patch_item_field_values(self, board_id: str, item_id: str, values: dict, **kwargs: Any) -> dict:
            captured.setdefault("patches", []).append(
                {"board_id": board_id, "item_id": item_id, "values": dict(values)}
            )
            return {"ok": True, "patched_keys": list(values)}

        async def get_task(self, task_id: str) -> dict:
            return {"ok": True, "task": {"id": task_id, "boardId": "board-from-task"}}

    monkeypatch.setattr("boardman.services.task_mutations.sync_team_assignment_field_keys_from_board", _noop_sync)
    monkeypatch.setattr("boardman.services.task_mutations.fetch_board_schema_bundle", _schema_bundle)
    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)
    monkeypatch.setattr("boardman.services.task_mutations.load_team_assignments", lambda: _cfg_for_route())

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_r = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Create before update",
                "description": "seed",
                "repo": "acme/widget",
                "plaky_board_id": "board-77",
                "plaky_group_id": "grp-1",
                "auto_assign_team": True,
            },
        )
        assert create_r.status_code == 200
        created = create_r.json()
        assert created.get("ok") is True, created

        update_comment = (
            "Updated fields:\n"
            "- engineer_plaky_id: eng-9\n"
            "- qa_plaky_id: qa-9\n"
            "- status: Needs QA\n"
            "- type: Bug\n"
            "- priority: Very Important\n"
            "- repo: acme/widget\n"
            "- github_repos: acme/widget, acme/api"
        )
        r = await client.patch(
            "/api/v1/tasks/item-123",
            json={
                "title": "Updated title",
                "comment": update_comment,
                "plaky_board_id": "board-77",
                "engineer_plaky_id": "eng-9",
                "qa_plaky_id": "qa-9",
                "status": "Needs QA",
                "type": "Bug",
                "priority": "Very Important",
                "repo": "acme/widget",
                "github_repos": ["acme/widget", "acme/api"],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body
    ops = body.get("operations") or {}
    assert (ops.get("title_update") or {}).get("ok") is True
    assert (ops.get("comment_add") or {}).get("ok") is True
    assert (ops.get("field_patch") or {}).get("ok") is True

    title_update = captured.get("title") or {}
    assert title_update.get("task_id") == "item-123"
    assert title_update.get("title") == "Updated title"
    comment = captured.get("comment") or {}
    assert comment.get("task_id") == "item-123"
    comment_body = str(comment.get("body") or "")
    assert "Updated fields:" in comment_body
    assert update_comment not in comment_body
    assert "status: Needs QA" in comment_body
    patches = captured.get("patches") or []
    assert len(patches) >= 2  # one from create flow, one from update flow
    update_patch = patches[-1]
    assert update_patch.get("board_id") == "board-77"
    assert update_patch.get("item_id") == "item-123"
    vals = update_patch.get("values") or {}
    assert vals.get("fld_eng") == "eng-9"
    assert vals.get("fld_qa") == "qa-9"
    assert vals.get("fld_status") == 30
    assert vals.get("fld_type") == 1
    assert vals.get("fld_pri") == 9
    assert "fld_repo" in vals
    assert "fld_repos_multi" in vals


@pytest.mark.asyncio
async def test_patch_tasks_rejects_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePlaky:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

    monkeypatch.setattr("boardman.services.task_mutations.PlakyClient", FakePlaky)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.patch("/api/v1/tasks/item-123", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("status") == 400
