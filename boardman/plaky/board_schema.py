"""Normalize Plaky board JSON into statuses / field options for prompts and tools."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional

# LLMs often invent Jira-like keys; reject before hitting Plaky.
_PLACEHOLDER_FIELD_KEY = re.compile(
    r"^(person|status|select|field|column|type|priority|user|assignee|dropdown)-\d+$",
    re.IGNORECASE,
)

from boardman.settings import settings

_schema_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_schema_lock = asyncio.Lock()


def clear_board_schema_cache() -> None:
    """Tests / hot reload."""
    _schema_cache.clear()


def _opt_label(o: Any) -> str:
    if isinstance(o, str):
        return o.strip()
    if isinstance(o, dict):
        for k in ("name", "label", "title", "value", "text", "slug", "id"):
            v = o.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _collect_options(field: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen_labels: set[str] = set()
    options: List[Dict[str, Any]] = []
    for key in ("options", "choices", "values", "statuses", "items", "allowedValues", "enum"):
        block = field.get(key)
        if not isinstance(block, list):
            continue
        for o in block:
            lab = _opt_label(o)
            if not lab or lab in seen_labels:
                continue
            seen_labels.add(lab)
            if isinstance(o, dict):
                # Keep the whole dict so we can see colors/types if present
                options.append(dict(o, name=lab))
            else:
                options.append({"name": lab})
        if options:
            break
    return options


def plaky_field_row_label(f: Dict[str, Any]) -> str:
    """Lowercase label for matching Plaky item fields across API shapes."""
    raw = (
        f.get("name")
        or f.get("title")
        or f.get("label")
        or f.get("fieldName")
        or f.get("key")
        or f.get("id")
        or ""
    )
    return str(raw).strip().lower()


def field_row_item_key(f: Dict[str, Any]) -> str:
    return str(
        f.get("key") or f.get("id") or f.get("fieldKey") or f.get("itemFieldKey") or f.get("field_id") or ""
    ).strip()


def field_likely_person_column(f: Dict[str, Any]) -> bool:
    """
    True if this field is probably a person/assignee column.
    Plaky often omits `type` or uses values we do not map — fall back to column name heuristics.
    """
    if not isinstance(f, dict):
        return False
    ftype = str(f.get("type") or f.get("fieldType") or f.get("kind") or "").strip()
    u = ftype.upper()
    if "PERSON" in u or u in (
        "USER",
        "USERS",
        "MEMBER",
        "MEMBERS",
        "PEOPLE",
        "ASSIGNEE",
        "ASSIGNEES",
        "MULTIUSER",
        "MULTI_USER",
    ):
        return True
    n = plaky_field_row_label(f)
    if not n:
        return False
    return any(
        tok in n
        for tok in (
            "qa",
            "quality",
            "engineer",
            "developer",
            "dev",
            "contributor",
            "assignee",
            "assignment",
            "owner",
            "person",
            "member",
            "people",
            "support",
            "lead",
        )
    )


def field_likely_github_repo_column(f: Dict[str, Any]) -> bool:
    """True if this field is probably a GitHub / repository text or link column."""
    if not isinstance(f, dict):
        return False
    n = plaky_field_row_label(f)
    if not n:
        return False
    return any(
        tok in n
        for tok in (
            "repo",
            "repository",
            "repositories",
            "github",
            "gitlab",
            "bitbucket",
            "codebase",
            "scm",
        )
    )


def _nested_field_meta(f: Dict[str, Any]) -> Dict[str, Any]:
    inner = f.get("field")
    return inner if isinstance(inner, dict) else {}


def _normalize_field_dict(f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    nested = _nested_field_meta(f)
    name = (
        f.get("name")
        or f.get("title")
        or f.get("label")
        or f.get("fieldName")
        or nested.get("name")
        or nested.get("title")
        or nested.get("label")
        or f.get("key")
        or f.get("fieldKey")
        or f.get("itemFieldKey")
        or nested.get("key")
        or ""
    )
    name = str(name).strip() if name else ""
    key = str(
        f.get("key")
        or f.get("id")
        or f.get("fieldKey")
        or f.get("itemFieldKey")
        or nested.get("key")
        or nested.get("id")
        or nested.get("fieldKey")
        or ""
    ).strip()
    if not name and key:
        name = key
    if not name:
        return None
    ftype = str(
        f.get("type") or f.get("fieldType") or f.get("kind") or nested.get("type") or nested.get("fieldType") or ""
    ).strip()
    options = _collect_options(f) or _collect_options(nested)
    return {"name": name, "type": ftype, "key": key, "options": options}


def _option_primary_patch_value(opt: Dict[str, Any]) -> Any:
    """Pick a PATCH-friendly value for a schema option dict (prefer stable ids over labels)."""
    for k in ("id", "optionId", "value", "_id"):
        raw = opt.get(k)
        if raw is None:
            continue
        if isinstance(raw, int):
            return raw
        s = str(raw).strip()
        if not s:
            continue
        if s.isdigit():
            return int(s)
        return raw
    lab = opt.get("name")
    if lab is not None and str(lab).strip():
        return str(lab).strip()
    return None


def select_field_patch_pair_from_schema(
    normalized: Optional[Dict[str, Any]],
    *,
    column_name_substrings: tuple[str, ...],
    value_label_candidates: tuple[str, ...],
    exclude_name_substrings: tuple[str, ...] = (),
) -> Optional[tuple[str, Any]]:
    """
    Find a select-like board field (non-empty `options`) whose label matches `column_name_substrings`,
    then resolve a board PATCH value from `value_label_candidates` (matched case-insensitively).
    Returns `(itemFieldKey, value)` or None.
    """
    if not isinstance(normalized, dict):
        return None
    raw_fields = normalized.get("fields")
    if not isinstance(raw_fields, list):
        return None
    subs = tuple(s for s in column_name_substrings if (s or "").strip())
    if not subs:
        return None
    excludes = tuple(e.casefold().strip() for e in exclude_name_substrings if (e or "").strip())
    wants = [c for c in value_label_candidates if (c or "").strip()]
    if not wants:
        return None
    want_cf = [c.casefold().strip() for c in wants]

    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        key = field_row_item_key(f)
        label = plaky_field_row_label(f)
        if not key or not label:
            continue
        if any(ex in label for ex in excludes):
            continue
        if not any(sub in label for sub in subs):
            continue
        options = f.get("options") or []
        ftype_u = str(f.get("type") or "").upper()
        if not isinstance(options, list) or not options:
            # Plaky item/board payloads often omit option lists for STATUS/SELECT; PATCH still accepts literals.
            if any(t in ftype_u for t in ("STATUS", "SELECT", "DROPDOWN")) and wants:
                return (key, value_label_candidates[0])
            continue
        for opt in options:
            if not isinstance(opt, dict):
                continue
            lab = str(opt.get("name") or "").casefold().strip()
            if not lab:
                continue
            for wc in want_cf:
                if lab == wc or wc in lab or lab in wc:
                    val = _option_primary_patch_value(opt)
                    if val is not None:
                        return (key, val)
    return None


def looks_like_placeholder_plaky_field_key(key: str) -> bool:
    """
    True for keys that *look* like LLM/template examples (person-1, status-2, …).

    Plaky boards often use the **same** patterns as real `itemFieldKey` values.
    Never treat a key as disposable based on this alone — compare to the board schema allowlist first.
    """
    k = (key or "").strip()
    return bool(k) and bool(_PLACEHOLDER_FIELD_KEY.match(k))


def validate_field_values_against_board_schema(
    field_values: Dict[str, Any],
    normalized: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Return an agent-visible error string if field keys are invalid; None if OK or not checkable.

    When the board schema lists non-empty field keys, every key in field_values must match.
    Placeholder-*pattern* keys are rejected only when they are not present on this board schema.
    """
    if not field_values:
        return None
    fields: List[Any] = []
    if isinstance(normalized, dict):
        raw = normalized.get("fields")
        if isinstance(raw, list):
            fields = raw
    allowed: set[str] = set()
    for f in fields:
        if not isinstance(f, dict):
            continue
        k = field_row_item_key(f)
        if k:
            allowed.add(k)

    bad_ph = [
        k
        for k in field_values
        if looks_like_placeholder_plaky_field_key(str(k)) and str(k).strip() not in allowed
    ]
    if bad_ph:
        return (
            "Refused: placeholder-like field keys "
            f"{bad_ph!r}. Call plaky_board_schema(board_id) and use real keys from the schema (key=`...`). "
            "Do not invent person-1 / status-2 style ids."
        )

    if allowed:
        unknown = [str(k) for k in field_values if str(k).strip() not in allowed]
        if unknown:
            return (
                "Refused: field keys not on this board schema: "
                f"{unknown!r}. Allowed keys: {sorted(allowed)!r}. "
                "Call plaky_board_schema(board_id), then retry with only those keys."
            )
    return None


def _deep_find_field_lists(obj: Any, *, depth: int = 0, max_depth: int = 6) -> List[List[Any]]:
    """Nested board JSON sometimes nests field definition lists under settings/meta blocks."""
    if depth > max_depth or not isinstance(obj, dict):
        return []
    out: List[List[Any]] = []
    for k, v in obj.items():
        lk = str(k).lower()
        if isinstance(v, list) and v and isinstance(v[0], dict):
            sample = v[0]
            has_sig = any(
                sample.get(x)
                for x in ("itemFieldKey", "fieldKey", "field", "fieldName", "boardFieldId", "itemFieldId")
            )
            if has_sig and any(
                t in lk for t in ("field", "column", "item", "board", "custom", "property", "definition")
            ):
                out.append(v)
        elif isinstance(v, dict):
            out.extend(_deep_find_field_lists(v, depth=depth + 1, max_depth=max_depth))
    return out


def field_stubs_from_board_items(items: List[Dict[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
    """
    When GET board omits field definitions, infer field keys + labels from item payloads
    (timeline/list API usually embeds itemFields / fields per item).
    """
    stubs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in (items or [])[:limit]:
        if not isinstance(item, dict):
            continue
        for group_key in (
            "itemFields",
            "fields",
            "boardItemFields",
            "item_fields",
            "customFields",
            "board_fields",
        ):
            block = item.get(group_key)
            if not isinstance(block, list):
                continue
            for raw in block:
                if not isinstance(raw, dict):
                    continue
                nf = _normalize_field_dict(raw)
                if not nf:
                    continue
                k = field_row_item_key(nf)
                if not k or k in seen:
                    continue
                seen.add(k)
                stubs.append(nf)
    return stubs


def merge_normalized_field_list(
    existing: List[Dict[str, Any]],
    additions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Append schema field rows from `additions` when their `key` is not already present."""
    by_k: Dict[str, Dict[str, Any]] = {}
    for f in existing or []:
        if isinstance(f, dict):
            nk = field_row_item_key(f)
            if nk:
                by_k[nk] = f
    for f in additions or []:
        if not isinstance(f, dict):
            continue
        nk = field_row_item_key(f)
        if nk and nk not in by_k:
            by_k[nk] = f
    return list(by_k.values())


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
            "itemFieldDefinitions",
            "boardFieldDefinitions",
            "spaceBoardFields",
        ):
            block = board_raw.get(key)
            if isinstance(block, list):
                field_lists.append(block)
        for extra in _deep_find_field_lists(board_raw):
            field_lists.append(extra)
        for block in field_lists:
            for f in block:
                if isinstance(f, dict):
                    nf = _normalize_field_dict(f)
                    if nf:
                        fields.append(nf)
    deduped: List[Dict[str, Any]] = []
    seen_key: set[str] = set()
    for f in fields:
        fk = field_row_item_key(f)
        if fk:
            if fk in seen_key:
                continue
            seen_key.add(fk)
        deduped.append(f)
    return {
        "board_name": board_name,
        "groups": [{"id": str(g.get("id", "")), "name": str(g.get("name", ""))} for g in groups if g.get("id")],
        "fields": deduped,
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
                opt_labels = []
                for o in opts:
                    lab = o.get("name") if isinstance(o, dict) else str(o)
                    if lab: opt_labels.append(lab)
                lines.append(f"  - Allowed values: {', '.join(opt_labels[:50])}")
                if len(opt_labels) > 50:
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

    ttl = float(settings.plaky_board_schema_cache_ttl_seconds or 0.0)
    if ttl > 0:
        now = time.monotonic()
        async with _schema_lock:
            hit = _schema_cache.get(bid)
            if hit is not None and (now - hit[0]) < ttl:
                return hit[1]

    c = PlakyClient()
    groups_r, board_r = await asyncio.gather(c.list_groups(bid), c.get_board(bid))
    groups = groups_r.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    board_raw = board_r.get("board") if board_r.get("ok") else None
    raw_keys: List[str] = []
    if isinstance(board_raw, dict):
        raw_keys = [str(k) for k in board_raw.keys()]

    normalized = normalize_board_payload(board_raw if isinstance(board_raw, dict) else None, groups)

    ok_board = bool(board_r.get("ok"))
    if ok_board and bid and c._public_root():
        try:
            listed = await c.list_board_items(bid, max_pages=1)
            rows = [x for x in (listed.get("items") or []) if isinstance(x, dict)]
            stubs = field_stubs_from_board_items(rows)
            if stubs:
                normalized["fields"] = merge_normalized_field_list(normalized.get("fields") or [], stubs)
        except Exception:
            pass
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
    result: dict[str, Any] = {
        "ok": ok,
        "message": message,
        "markdown": md,
        "normalized": normalized,
        "board_fetch_ok": ok_board,
        "groups_fetch_ok": ok_groups,
    }
    if ttl > 0:
        async with _schema_lock:
            _schema_cache[bid] = (time.monotonic(), result)
    return result
