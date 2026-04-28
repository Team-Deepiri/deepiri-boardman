"""
Live Plaky API tests — uses PLAKY_API_KEY from `.env` (loaded in conftest) or the shell.

Run:
  poetry run pytest tests/test_plaky_live.py -v --tb=short

Optional write (creates one task):
  PLAKY_LIVE_WRITE=1 poetry run pytest tests/test_plaky_live.py -v -k create_item
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from boardman.main import create_app
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows
from boardman.settings import settings

pytestmark = pytest.mark.plaky_live


def _plaky_configured() -> bool:
    return bool((settings.plaky_api_key or "").strip())


skip_no_plaky = pytest.mark.skipif(
    not _plaky_configured(),
    reason="PLAKY_API_KEY missing — set in repo .env or export before pytest",
)


def _pick_board_id(boards: list) -> str:
    if settings.plaky_default_board_id.strip():
        return settings.plaky_default_board_id.strip()
    assert boards, "list_boards returned no boards"
    return str(boards[0]["id"])


def _find_named_row(rows: list[dict], target_name: str) -> dict | None:
    want = (target_name or "").strip().lower()
    if not want:
        return None
    exact = next((r for r in rows if str(r.get("name") or "").strip().lower() == want), None)
    if exact:
        return exact
    return next((r for r in rows if want in str(r.get("name") or "").strip().lower()), None)


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_list_boards():
    c = PlakyClient()
    r = await c.list_boards()
    assert r.get("ok") is True, (
        f"list_boards failed: status={r.get('status')} message={r.get('message')!r} "
        f"(check PLAKY_API_BASE={settings.plaky_api_base!r})"
    )
    boards = r.get("boards") or []
    assert isinstance(boards, list)
    for b in boards[:5]:
        assert b.get("id")
        assert isinstance(b.get("name"), str)


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_list_groups():
    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True, br.get("message")
    board_id = _pick_board_id(br["boards"])
    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, (
        f"list_groups failed for board_id={board_id!r}: status={gr.get('status')} "
        f"message={gr.get('message')!r}"
    )
    groups = gr.get("groups") or []
    assert isinstance(groups, list)
    for g in groups[:5]:
        assert g.get("id")


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_get_board():
    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True
    board_id = _pick_board_id(br["boards"])
    r = await c.get_board(board_id)
    assert r.get("ok") is True, r.get("message")
    assert isinstance(r.get("board"), dict)


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_board_schema_bundle():
    br = await PlakyClient().list_boards()
    assert br.get("ok") is True
    board_id = _pick_board_id(br["boards"])
    bundle = await fetch_board_schema_bundle(board_id)
    assert bundle.get("ok") is True, bundle.get("message")
    assert bundle.get("normalized") is not None


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_name_match_boards():
    c = PlakyClient()
    r = await c.list_boards()
    assert r.get("ok") is True
    boards = r.get("boards") or []
    matches, best = rank_plaky_rows(boards, "")
    assert len(matches) == len(boards)
    if boards:
        full_name = str(boards[0].get("name") or "").strip()
        if full_name:
            m2, _ = rank_plaky_rows(boards, full_name)
            assert m2[0]["id"] == str(boards[0]["id"])
            assert m2[0]["score"] >= 700


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_http_boards_match_route():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/plaky/boards/match", params={"query": "a"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True, body.get("message")
        assert "matches" in body and "boards" in body


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_http_board_schema_route():
    br = await PlakyClient().list_boards()
    assert br.get("ok") is True
    board_id = _pick_board_id(br["boards"])
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/api/v1/plaky/boards/{board_id}/schema")
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True, body.get("message")


@skip_no_plaky
@pytest.mark.asyncio
async def test_live_get_tasks_smoke():
    """Legacy /tasks listing — may be empty; should not 401 if key is valid."""
    c = PlakyClient()
    r = await c.get_tasks(status="open")
    st = r.get("status")
    assert st != 401, "get_tasks returned 401 — PLAKY_API_KEY rejected"
    assert r.get("ok") is True or st in (404, 422), (
        f"get_tasks: {r.get('message')!r} status={st}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PLAKY_LIVE_WRITE") != "1",
    reason="Set PLAKY_LIVE_WRITE=1 to create a real Plaky item (uses default or first board/group)",
)
async def test_live_create_item_hierarchy():
    if not _plaky_configured():
        pytest.skip("PLAKY_API_KEY missing")
    c = PlakyClient()
    br = await c.list_boards()
    assert br.get("ok") is True
    board_id = _pick_board_id(br["boards"])
    gr = await c.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    gid = (settings.plaky_default_group_id or "").strip()
    if not gid:
        assert groups, "need at least one group or PLAKY_DEFAULT_GROUP_ID"
        gid = str(groups[0]["id"])
    app = create_app()
    title = "[boardman pytest PLAKY_LIVE_WRITE] delete me"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tasks",
            json={
                "title": title,
                "description": "Automated test task; safe to delete.",
                "repo": "deepiri-platform",
                "plaky_board_id": board_id,
                "plaky_group_id": gid,
                "auto_assign_team": False,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True, body.get("message")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PLAKY_LIVE_WRITE") != "1",
    reason="Set PLAKY_LIVE_WRITE=1 to run live create+update route test in Boardman Test Board/Sprint 2",
)
async def test_live_create_then_update_task_in_boardman_test_board_sprint_2():
    if not _plaky_configured():
        pytest.skip("PLAKY_API_KEY missing")

    plaky = PlakyClient()
    br = await plaky.list_boards()
    assert br.get("ok") is True, br.get("message")
    boards = br.get("boards") or []
    assert isinstance(boards, list) and boards, "No boards returned from Plaky"
    board = _find_named_row(boards, "Boardman Test Board")
    assert board is not None, "Could not find 'Boardman Test Board'"
    board_id = str(board["id"])

    gr = await plaky.list_groups(board_id)
    assert gr.get("ok") is True, gr.get("message")
    groups = gr.get("groups") or []
    assert isinstance(groups, list) and groups, "No groups returned for Boardman Test Board"
    group = _find_named_row(groups, "Sprint 2")
    assert group is not None, "Could not find 'Sprint 2' group on Boardman Test Board"
    group_id = str(group["id"])

    title = "[boardman pytest live create+update] Boardman Test Board / Sprint 2"
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_r = await client.post(
            "/api/v1/tasks",
            json={
                "title": title,
                "description": "Live test task for create->update route verification.",
                "repo": "team-deepiri/boardman",
                "plaky_board_id": board_id,
                "plaky_group_id": group_id,
                "auto_assign_team": False,
            },
        )
        assert create_r.status_code == 200
        create_body = create_r.json()
        assert create_body.get("ok") is True, create_body
        task_id = str(
            create_body.get("task_id")
            or ((create_body.get("task") or {}).get("id") if isinstance(create_body.get("task"), dict) else "")
            or ""
        ).strip()
        assert task_id, f"Could not resolve task id from create response: {create_body}"

        update_comment = (
            "Updated field values via PATCH /api/v1/tasks/{task_id}:\n"
            "- status: Needs QA\n"
            "- type: Bug\n"
            "- priority: Very Important\n"
            "- repo: team-deepiri/boardman\n"
            "- github_repos: team-deepiri/boardman, team-deepiri/deepiri-boardman"
        )
        patch_r = await client.patch(
            f"/api/v1/tasks/{task_id}",
            json={
                "comment": update_comment,
                "plaky_board_id": board_id,
                "status": "Needs QA",
                "type": "Bug",
                "priority": "Very Important",
                "repo": "team-deepiri/boardman",
                "github_repos": [
                    "team-deepiri/boardman",
                    "team-deepiri/deepiri-boardman",
                ],
            },
        )
    assert patch_r.status_code == 200
    patch_body = patch_r.json()
    assert patch_body.get("ok") is True, patch_body
    ops = patch_body.get("operations") or {}
    assert (ops.get("comment_add") or {}).get("ok") is True
    assert (ops.get("field_patch") or {}).get("ok") is True
