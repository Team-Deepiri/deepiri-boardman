"""Normalize Plaky board JSON into statuses / field options for prompts and tools."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _opt_label(o: Any) -> str:
    if isinstance(o, str):
        return o.strip()
    if isinstance(o, dict):
        for k in ("name", "label", "title", "value", "text", "slug", "id"):
            v = o.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _collect_options(field: Dict[str, Any]) -> List[str]:
    seen: List[str] = []
    for key in ("options", "choices", "values", "statuses", "items", "allowedValues", "enum"):
        block = field.get(key)
        if not isinstance(block, list):
            continue
        for o in block:
            lab = _opt_label(o)
            if lab and lab not in seen:
                seen.append(lab)
        if seen:
            break
    return seen


def _normalize_field_dict(f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (
        f.get("name")
        or f.get("title")
        or f.get("label")
        or f.get("fieldName")
        or f.get("key")
        or ""
    )
    name = str(name).strip() if name else ""
    if not name:
        return None
    ftype = str(f.get("type") or f.get("fieldType") or f.get("kind") or "").strip()
    options = _collect_options(f)
    key = str(f.get("key") or f.get("id") or f.get("fieldKey") or "").strip()
    return {"name": name, "type": ftype, "key": key, "options": options}


def normalize_board_payload(board_raw: Optional[Dict[str, Any]], groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Extract board name, groups, and field definitions with option lists when present.
    Works across varying Plaky API shapes by scanning common keys.
    """
    board_name = ""
    fields: List[Dict[str, Any]] = []
    if isinstance(board_raw, dict):
        board_name = str(board_raw.get("name") or board_raw.get("title") or "").strip()
        field_lists: List[Any] = []
        for key in (
            "itemFields",
            "item_fields",
            "fields",
            "boardFields",
            "board_fields",
            "customFields",
            "custom_fields",
            "columns",
            "fieldDefinitions",
        ):
            block = board_raw.get(key)
            if isinstance(block, list):
                field_lists.append(block)
        for block in field_lists:
            for f in block:
                if isinstance(f, dict):
                    nf = _normalize_field_dict(f)
                    if nf:
                        fields.append(nf)
    return {
        "board_name": board_name,
        "groups": [{"id": str(g.get("id", "")), "name": str(g.get("name", ""))} for g in groups if g.get("id")],
        "fields": fields,
    }


def format_board_schema_markdown(
    board_id: str,
    *,
    ok: bool,
    message: str = "",
    normalized: Optional[Dict[str, Any]] = None,
    raw_top_keys: Optional[List[str]] = None,
) -> str:
    """Markdown block appended to the system prompt."""
    lines = [
        "",
        "## Current Plaky board schema (from API)",
        f"**Board id:** `{board_id}`",
    ]
    if not ok:
        lines.append(f"Schema could not be loaded ({message or 'unknown error'}).")
        lines.append(
            "Call **plaky_board_schema** with this `board_id` after **plaky_match_board** to refresh, "
            "or check API key / board access."
        )
        return "\n".join(lines)

    n = normalized or {}
    if n.get("board_name"):
        lines.append(f"**Board name:** {n['board_name']}")
    groups = n.get("groups") or []
    if groups:
        lines.append("**Groups (sections / swimlanes):**")
        for g in groups[:80]:
            lines.append(f"- `{g.get('id')}` — {g.get('name', '')}")
    fields = n.get("fields") or []
    if fields:
        lines.append("**Fields (use these labels/values when describing or updating items — match API literals):**")
        for f in fields[:60]:
            opts = f.get("options") or []
            typ = f.get("type") or ""
            key = f.get("key") or ""
            suffix = f" ({typ})" if typ else ""
            key_part = f" key=`{key}`" if key else ""
            lines.append(f"- **{f.get('name', 'field')}**{suffix}{key_part}")
            if opts:
                lines.append(f"  - Allowed values: {', '.join(opts[:50])}")
                if len(opts) > 50:
                    lines.append("  - …")
    else:
        lines.append(
            "**Fields:** No field definitions were returned on this board payload. "
            "Infer allowed **status** / **priority** / custom values from **plaky_get_task** on an existing item, "
            "or from Plaky UI labels (API may use slugs)."
        )
        if raw_top_keys:
            lines.append(f"*Raw board JSON top-level keys:* `{', '.join(raw_top_keys[:40])}`")
    lines.append(
        "When calling **plaky_create_task** (field_values_json), **plaky_patch_item_fields**, or **plaky_update_task**, "
        "use field keys from above and values that match this board (exact literals, option ids, or assignee ids as Plaky expects)."
    )
    return "\n".join(lines)


async def fetch_board_schema_bundle(board_id: str) -> Dict[str, Any]:
    """Load groups + board detail and return normalized schema + markdown for prompts."""
    from boardman.plaky.client import PlakyClient

    bid = (board_id or "").strip()
    if not bid:
        return {
            "ok": False,
            "message": "board_id is empty",
            "markdown": format_board_schema_markdown("", ok=False, message="board_id is empty"),
            "normalized": None,
        }

    c = PlakyClient()
    groups_r = await c.list_groups(bid)
    groups = groups_r.get("groups") or []
    if not isinstance(groups, list):
        groups = []

    board_r = await c.get_board(bid)
    board_raw = board_r.get("board") if board_r.get("ok") else None
    raw_keys: List[str] = []
    if isinstance(board_raw, dict):
        raw_keys = [str(k) for k in board_raw.keys()]

    normalized = normalize_board_payload(board_raw if isinstance(board_raw, dict) else None, groups)

    ok_board = bool(board_r.get("ok"))
    ok_groups = bool(groups_r.get("ok"))
    ok = ok_board or ok_groups
    msg_parts = []
    if not ok_board and board_r.get("message"):
        msg_parts.append(f"board: {board_r.get('message')}")
    if not ok_groups and groups_r.get("message"):
        msg_parts.append(f"groups: {groups_r.get('message')}")
    message = "; ".join(msg_parts) if msg_parts else ""

    md = format_board_schema_markdown(
        bid,
        ok=ok,
        message=message,
        normalized=normalized,
        raw_top_keys=raw_keys if not normalized.get("fields") else None,
    )
    return {
        "ok": ok,
        "message": message,
        "markdown": md,
        "normalized": normalized,
        "board_fetch_ok": ok_board,
        "groups_fetch_ok": ok_groups,
    }
