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


def looks_like_placeholder_plaky_field_key(key: str) -> bool:
    """True for invented keys such as person-1, status-2 (not from plaky_board_schema)."""
    k = (key or "").strip()
    return bool(k) and bool(_PLACEHOLDER_FIELD_KEY.match(k))


def _field_option_strings(field: Dict[str, Any]) -> List[str]:
    """Option display strings for a normalized field (dict options use _opt_label)."""
    opts = field.get("options")
    if not isinstance(opts, list):
        return []
    out: List[str] = []
    for x in opts:
        if isinstance(x, dict):
            lab = _opt_label(x)
            if lab:
                out.append(lab)
        elif str(x).strip():
            out.append(str(x).strip())
    return out


def match_field_option_value(options: List[str], value: Any) -> tuple[Any, Optional[str]]:
    """
    Match a user/model value to an allowed option (case-insensitive).
    Non-string values pass through without matching when options is non-empty (caller may coerce).
    """
    if not options or not isinstance(value, str):
        return value if value is None or isinstance(value, str) else str(value), None
    v = value.strip()
    if not v:
        return "", None
    by_casefold = {str(opt).strip().casefold(): str(opt).strip() for opt in options if str(opt).strip()}
    hit = by_casefold.get(v.casefold())
    if hit is not None:
        return hit, None
    return None, f"value {value!r} not in allowed options: {options[:20]}"


def validate_field_values_detailed(
    field_values: Dict[str, Any],
    normalized: Optional[Dict[str, Any]],
    *,
    options_check: bool = False,
    board_id: str = "",
    schema_fetch_ok: Optional[bool] = None,
    schema_fetch_message: str = "",
) -> tuple[Dict[str, Any], List[str], List[str]]:
    """
    Validate field keys (and optionally enum/select values) against normalized board schema.

    Returns ``(cleaned_values, errors, warnings)``. On any error, ``cleaned_values`` is empty.
    """
    warnings: List[str] = []
    if not field_values:
        return {}, [], []

    bid = (board_id or "").strip()
    fields: List[Any] = []
    if isinstance(normalized, dict):
        raw = normalized.get("fields")
        if isinstance(raw, list):
            fields = raw

    allowed: set[str] = set()
    by_key: Dict[str, Dict[str, Any]] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        k = str(f.get("key") or "").strip()
        if k:
            allowed.add(k)
            by_key[k] = f

    errors: List[str] = []

    bad_ph = [k for k in field_values if looks_like_placeholder_plaky_field_key(str(k))]
    if bad_ph:
        errors.append(
            "Refused: placeholder-like field keys "
            f"{bad_ph!r}. Call plaky_board_schema(board_id) and use real keys from the schema (key=`...`). "
            "Do not invent person-1 / status-2 style ids."
        )

    if allowed:
        unknown = [str(k) for k in field_values if str(k).strip() not in allowed]
        if unknown:
            errors.append(
                "Refused: field keys not on this board schema: "
                f"{unknown!r}. Allowed keys: {sorted(allowed)!r}. "
                "Call plaky_board_schema(board_id), then retry with only those keys."
            )

    if errors:
        return {}, errors, warnings

    if schema_fetch_ok is not None and schema_fetch_ok is not True:
        warnings.append(f"schema bundle returned warning: {schema_fetch_message or 'unknown'}")

    if not by_key:
        if field_values:
            if not fields:
                warnings.append("board schema had no field definitions; skipped key/value validation")
            else:
                warnings.append("board schema had no field keys; skipped key/value validation")
        return dict(field_values), [], warnings

    cleaned: Dict[str, Any] = {}
    for k, v in field_values.items():
        ks = str(k).strip()
        if not ks or ks not in by_key:
            continue
        field = by_key[ks]
        if options_check:
            opt_strs = _field_option_strings(field)
            if opt_strs:
                matched, err = match_field_option_value(opt_strs, v)
                if err:
                    errors.append(f"{ks}: {err}")
                    continue
                cleaned[ks] = matched
            else:
                cleaned[ks] = v
        else:
            cleaned[ks] = v

    if errors:
        return {}, errors, warnings

    return cleaned, [], warnings


def validate_field_values_against_board_schema(
    field_values: Dict[str, Any],
    normalized: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Return an agent-visible error string if field keys are invalid; None if OK or not checkable.

    When the board schema lists non-empty field keys, every key in field_values must match.
    Placeholder-style keys are always rejected.
    """
    _, errors, _ = validate_field_values_detailed(
        field_values, normalized, options_check=False, board_id=""
    )
    return errors[0] if errors else None


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
