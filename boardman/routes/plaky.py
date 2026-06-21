"""Plaky hierarchy: boards and groups (for UI + API discovery)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter

from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.plaky.name_match import rank_plaky_rows
from boardman.settings import settings

router = APIRouter()


@router.get("/llm/models")
async def list_llm_models() -> dict:
    """
    List available LLM models based on provider setting.
    For Ollama: fetches from /api/tags.
    For other providers: returns configured model or empty.
    """
    provider = (settings.llm_provider or "ollama").lower()

    if provider == "ollama":
        base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{base_url}/api/tags")
                if r.status_code == 200:
                    data = r.json()
                    models = data.get("models", []) or []
                    model_list = []
                    for m in models:
                        name = m.get("name") or m.get("model") or ""
                        if name:
                            model_list.append(
                                {
                                    "name": name,
                                    "size": m.get("size"),
                                    "details": m.get("details", {}),
                                }
                            )
                    # Get current model
                    from boardman.llm.ollama_autodetect import effective_ollama_model

                    current = effective_ollama_model(None)
                    return {
                        "ok": True,
                        "provider": "ollama",
                        "models": model_list,
                        "current": current,
                    }
        except Exception as e:
            return {"ok": False, "provider": "ollama", "models": [], "error": str(e)}

    # Non-Ollama providers
    return {
        "ok": True,
        "provider": provider,
        "models": [],
        "current": (settings.llm_model or "").strip() or None,
    }


@router.get("/plaky/users")
async def plaky_workspace_users(query: str = "") -> dict:
    """Workspace users for assignee pickers (Plaky GET /v1/public/users)."""
    c = PlakyClient()
    r = await c.list_workspace_users()
    users = r.get("users") or []
    if not isinstance(users, list):
        users = []
    if not query.strip():
        return {
            "ok": r.get("ok"),
            "message": r.get("message"),
            "users": users,
            "matches": [],
            "best": None,
        }
    matches, best = rank_plaky_rows(users, query)
    return {
        "ok": r.get("ok"),
        "message": r.get("message"),
        "users": users,
        "matches": matches,
        "best": best,
    }


@router.get("/plaky/boards")
async def plaky_boards() -> dict:
    c = PlakyClient()
    r = await c.list_boards()
    return {"ok": r.get("ok"), "boards": r.get("boards", []), "message": r.get("message")}


@router.get("/plaky/boards/match")
async def plaky_boards_match(query: str = "") -> dict:
    """
    List boards via Plaky API, then rank by name against `query` (e.g. what the user said:
    "put this on the Deepiri Main board"). Empty `query` returns boards unranked (all score 0).
    """
    c = PlakyClient()
    r = await c.list_boards()
    boards = r.get("boards") or []
    if not isinstance(boards, list):
        boards = []
    matches, best = rank_plaky_rows(boards, query)
    return {
        "ok": r.get("ok"),
        "message": r.get("message"),
        "boards": boards,
        "matches": matches,
        "best": best,
    }


@router.get("/plaky/boards/{board_id}/schema")
async def plaky_board_schema(board_id: str) -> dict:
    """Groups + normalized field options (status/type/priority, etc.) for prompts and debugging."""
    bundle = await fetch_board_schema_bundle(board_id)
    return {
        "ok": bundle.get("ok"),
        "message": bundle.get("message"),
        "board_id": board_id,
        "board_fetch_ok": bundle.get("board_fetch_ok"),
        "groups_fetch_ok": bundle.get("groups_fetch_ok"),
        "normalized": bundle.get("normalized"),
        "markdown": bundle.get("markdown"),
    }


@router.get("/plaky/boards/{board_id}/groups")
async def plaky_board_groups(board_id: str) -> dict:
    c = PlakyClient()
    r = await c.list_groups(board_id)
    return {"ok": r.get("ok"), "groups": r.get("groups", []), "message": r.get("message")}


@router.get("/plaky/boards/{board_id}/groups/match")
async def plaky_board_groups_match(board_id: str, query: str = "") -> dict:
    """List groups on a board, rank names against `query` (e.g. 'Backlog', 'AI Bugs')."""
    c = PlakyClient()
    r = await c.list_groups(board_id)
    groups = r.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    matches, best = rank_plaky_rows(groups, query)
    return {
        "ok": r.get("ok"),
        "message": r.get("message"),
        "groups": groups,
        "matches": matches,
        "best": best,
    }
