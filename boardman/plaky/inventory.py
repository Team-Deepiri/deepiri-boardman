"""Plaky inventory helpers for deployment configuration handoff."""

from __future__ import annotations

from typing import Any

from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient


def compact_option(option: Any) -> dict[str, Any]:
    if isinstance(option, dict):
        out: dict[str, Any] = {}
        for key in ("id", "optionId", "value", "_id", "name", "label", "title", "color"):
            value = option.get(key)
            if value is not None and str(value).strip():
                out[key] = value
        if "name" not in out:
            for key in ("label", "title", "value", "id", "optionId", "_id"):
                value = option.get(key)
                if value is not None and str(value).strip():
                    out["name"] = str(value).strip()
                    break
        return out
    label = str(option or "").strip()
    return {"name": label} if label else {}


def compact_field(field: dict[str, Any]) -> dict[str, Any]:
    options = [
        compacted
        for option in field.get("options") or []
        if (compacted := compact_option(option))
    ]
    return {
        "key": str(field.get("key") or "").strip(),
        "name": str(field.get("name") or "").strip(),
        "type": str(field.get("type") or "").strip(),
        "options": options,
    }


def field_looks_status_like(field: dict[str, Any]) -> bool:
    name = str(field.get("name") or "").casefold()
    ftype = str(field.get("type") or "").casefold()
    return "status" in name or "status" in ftype


async def collect_plaky_inventory(
    *,
    board_id: str = "",
    include_users: bool = True,
) -> dict[str, Any]:
    """Collect board/group/field/status IDs without exposing the API key."""
    client = PlakyClient()
    boards_result = await client.list_boards()
    boards = boards_result.get("boards") if boards_result.get("ok") else []
    if not isinstance(boards, list):
        boards = []

    inventory: dict[str, Any] = {
        "ok": bool(boards_result.get("ok")),
        "boards": boards,
        "board": None,
        "groups": [],
        "fields": [],
        "status_fields": [],
        "users": [],
        "messages": [],
    }
    if boards_result.get("message"):
        inventory["messages"].append(str(boards_result["message"]))

    bid = (board_id or "").strip()
    if bid:
        selected = next((b for b in boards if str(b.get("id") or "").strip() == bid), None)
        inventory["board"] = selected or {"id": bid, "name": ""}
        schema = await fetch_board_schema_bundle(bid)
        if schema.get("message"):
            inventory["messages"].append(str(schema["message"]))
        normalized = schema.get("normalized") if isinstance(schema, dict) else None
        if isinstance(normalized, dict):
            groups = normalized.get("groups") or []
            fields = normalized.get("fields") or []
            inventory["groups"] = [g for g in groups if isinstance(g, dict)]
            compacted_fields = [compact_field(f) for f in fields if isinstance(f, dict)]
            inventory["fields"] = compacted_fields
            inventory["status_fields"] = [
                field for field in compacted_fields if field_looks_status_like(field)
            ]
        inventory["ok"] = bool(inventory["ok"] and schema.get("ok"))

    if include_users:
        users_result = await client.list_workspace_users()
        if users_result.get("ok"):
            users = users_result.get("users") or []
            inventory["users"] = [u for u in users if isinstance(u, dict)]
        elif users_result.get("message"):
            inventory["messages"].append(str(users_result["message"]))

    return inventory
