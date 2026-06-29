"""Normalize Plaky board JSON into statuses / field options for prompts and tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

# LLMs often invent Jira-like keys; reject before hitting Plaky.
_PLACEHOLDER_FIELD_KEY = re.compile(
    r"^(person|status|select|field|column|type|priority|user|assignee|dropdown)-\d+$",
    re.IGNORECASE,
)

from boardman.settings import settings

_schema_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_schema_lock = asyncio.Lock()


def clear_board_schema_cache() -> None:
    """Tests / hot reload (in-process only; Redis entries expire by TTL)."""
    _schema_cache.clear()


def _schema_redis_key(board_id: str) -> str:
    return f"boardman:plaky_schema_bundle:{board_id}"


def _opt_label(o: Any) -> str:
    if isinstance(o, str):
        return o.strip()
    if isinstance(o, dict):
        for k in ("name", "label", "title", "value", "text", "slug", "id"):
            v = o.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _collect_options(field: dict[str, Any]) -> list[dict[str, Any]]:
    seen_labels: set[str] = set()
    options: list[dict[str, Any]] = []
    for key in (
        "options",
        "choices",
        "values",
        "statuses",
        "items",
        "allowedValues",
        "enum",
        "tags",
        "tagOptions",
        "tagValues",
        "possibleValues",
    ):
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


def plaky_field_row_label(f: dict[str, Any]) -> str:
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


def field_row_item_key(f: dict[str, Any]) -> str:
    return str(
        f.get("key")
        or f.get("id")
        or f.get("fieldKey")
        or f.get("itemFieldKey")
        or f.get("field_id")
        or ""
    ).strip()


def field_likely_person_column(f: dict[str, Any]) -> bool:
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


def field_likely_github_repo_column(f: dict[str, Any]) -> bool:
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


def _nested_field_meta(f: dict[str, Any]) -> dict[str, Any]:
    inner = f.get("field")
    return inner if isinstance(inner, dict) else {}


def _normalize_field_dict(f: dict[str, Any]) -> dict[str, Any] | None:
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
        f.get("type")
        or f.get("fieldType")
        or f.get("kind")
        or nested.get("type")
        or nested.get("fieldType")
        or ""
    ).strip()
    options = _collect_options(f) or _collect_options(nested)
    return {"name": name, "type": ftype, "key": key, "options": options}


def _option_primary_patch_value(opt: dict[str, Any]) -> Any:
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
    normalized: dict[str, Any] | None,
    *,
    column_name_substrings: tuple[str, ...],
    value_label_candidates: tuple[str, ...],
    exclude_name_substrings: tuple[str, ...] = (),
) -> tuple[str, Any] | None:
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


def _field_type_upper(f: dict[str, Any]) -> str:
    nested = f.get("field") if isinstance(f.get("field"), dict) else {}
    return str(
        f.get("type")
        or f.get("fieldType")
        or f.get("kind")
        or nested.get("type")
        or nested.get("fieldType")
        or ""
    ).upper()


def field_is_plaky_tag_column(f: dict[str, Any]) -> bool:
    return "TAG" in _field_type_upper(f)


def plaky_repo_field_value_format(
    normalized: dict[str, Any] | None,
    item_field_key: str,
) -> str:
    """
    Plaky TAG options are usually **repository names** (``deepiri-platform``), not ``owner/repo``.

    Return ``short`` when this field is a TAG column so callers can format patch values accordingly;
    otherwise ``full`` (keep ``owner/repo`` for text/link-style repo columns).

    When the board schema is missing or omits the field row, keys matching Plaky's native pattern
    ``tag-`` + digits (e.g. ``tag-2``) are treated as TAG columns so repo values still shorten.
    """
    k = (item_field_key or "").strip()
    if not k:
        return "full"
    native_tag_key = bool(re.match(r"^tag-\d+$", k, re.IGNORECASE))
    if isinstance(normalized, dict):
        for f in normalized.get("fields") or []:
            if isinstance(f, dict) and field_row_item_key(f) == k:
                return "short" if field_is_plaky_tag_column(f) else "full"
        if native_tag_key:
            return "short"
    elif native_tag_key:
        return "short"
    return "full"


def _norm_tag_compare_token(s: str) -> str:
    t = (s or "").strip().casefold().replace(" ", "")
    if t.startswith("#"):
        t = t[1:]
    return t


def _repo_tokens_from_assignment_value(raw: Any) -> list[str]:
    """Split comma-joined owner/repo strings from assignment field map values."""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) > 1:
            return [p for p in parts if "/" in p] or parts
        return [s] if s else []
    return []


def match_repo_tokens_to_plaky_tag_option_values(
    field: dict[str, Any],
    tokens: list[str],
) -> tuple[list[Any] | None, list[str]]:
    """
    Map GitHub owner/repo strings to TAG field option ids (or primary patch literals).

    Plaky TAG columns only persist values that match predefined tags on the board; free-text
    PATCH often returns 200 with an empty tag list.

    Returns ``(matched_values, unmatched_tokens)``. ``matched_values`` is None when nothing matched.
    """
    if not tokens:
        return None, []
    options = field.get("options") or []
    if not isinstance(options, list) or not options:
        return None, list(tokens)

    matched: list[Any] = []
    seen: set[Any] = set()
    unmatched: list[str] = []

    for token in tokens:
        nt = _norm_tag_compare_token(token)
        short = _norm_tag_compare_token(token.split("/")[-1]) if "/" in token else nt
        hit: Any = None
        for opt in options:
            if not isinstance(opt, dict):
                continue
            lab_raw = _opt_label(opt)
            if not lab_raw:
                continue
            nl = _norm_tag_compare_token(lab_raw)
            if not nl:
                continue
            if nt == nl or nt in nl or nl in nt or short == nl or short in nl or nl in short:
                hit = _option_primary_patch_value(opt)
                if hit is not None:
                    break
        if hit is not None:
            if hit not in seen:
                seen.add(hit)
                matched.append(hit)
        else:
            unmatched.append(token)

    return (matched if matched else None), unmatched


def resolve_repo_tag_field_values_from_schema(
    field_values: dict[str, Any],
    normalized: dict[str, Any] | None,
    *,
    keys: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    For Plaky TAG columns in ``keys``, replace GitHub repo strings with option ids from ``normalized``.

    Mutates ``field_values`` in place. Returns ``(field_values, warnings)`` where each warning is a
    small dict (``field_key``, ``token``, ``message``).
    """
    warnings: list[dict[str, Any]] = []
    if not field_values or not normalized or not keys:
        return field_values, warnings

    raw_fields = normalized.get("fields")
    if not isinstance(raw_fields, list):
        return field_values, warnings

    by_key: dict[str, dict[str, Any]] = {}
    for f in raw_fields:
        if isinstance(f, dict):
            ik = field_row_item_key(f)
            if ik:
                by_key[ik] = f

    for fk in keys:
        fk = (fk or "").strip()
        if not fk or fk not in field_values:
            continue
        fdef = by_key.get(fk)
        if not fdef or not field_is_plaky_tag_column(fdef):
            continue
        raw_val = field_values.get(fk)
        tokens = _repo_tokens_from_assignment_value(raw_val)
        if not tokens:
            continue
        resolved, unmatched = match_repo_tokens_to_plaky_tag_option_values(fdef, tokens)
        opts = fdef.get("options") or []
        if not isinstance(opts, list) or not opts:
            warnings.append(
                {
                    "field_key": fk,
                    "token": "",
                    "message": "TAG field has no option list on the board schema; cannot map repo to tag ids.",
                }
            )
            continue
        if resolved is not None:
            field_values[fk] = resolved
        for u in unmatched:
            warnings.append(
                {
                    "field_key": fk,
                    "token": u,
                    "message": "No Plaky tag option matched this repo string (add the tag on the board or align spelling).",
                }
            )
    return field_values, warnings


def looks_like_placeholder_plaky_field_key(key: str) -> bool:
    """
    True for keys that *look* like LLM/template examples (person-1, status-2, …).

    Plaky boards often use the **same** patterns as real `itemFieldKey` values.
    Never treat a key as disposable based on this alone — compare to the board schema allowlist first.
    """
    k = (key or "").strip()
    return bool(k) and bool(_PLACEHOLDER_FIELD_KEY.match(k))


def validate_field_values_against_board_schema(
    field_values: dict[str, Any],
    normalized: dict[str, Any] | None,
) -> str | None:
    """
    Return an agent-visible error string if field keys are invalid; None if OK or not checkable.

    When the board schema lists non-empty field keys, every key in field_values must match.
    Placeholder-*pattern* keys are rejected only when they are not present on this board schema.
    """
    if not field_values:
        return None
    fields: list[Any] = []
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


def _field_option_strings(field: dict[str, Any]) -> list[str]:
    """Display strings for a normalized field's options.

    Returns ``[]`` when the field is not options-bearing (free text, person,
    item, date, …) or when the bundle did not include option metadata.
    """
    out: list[str] = []
    for opt in _collect_options(field):
        lab = _opt_label(opt) if isinstance(opt, dict) else str(opt).strip()
        if lab:
            out.append(lab)
    return out


def match_field_option_value(options: list[str], value: Any) -> tuple[Any, str | None]:
    """Case-insensitive option matching.

    Returns ``(canonical_or_passthrough_value, error_or_None)``.
    Non-string values pass through (Plaky accepts ids/numbers literally).
    Empty string passes through unchanged. Unknown string values return
    ``(None, "<reason>")`` so the caller can surface a friendly rejection
    that lists the allowed options.
    """
    if not options or not isinstance(value, str):
        return (value if isinstance(value, str) or value is None else str(value)), None
    v = value.strip()
    if not v:
        return "", None
    by_cf = {opt.casefold(): opt for opt in options if opt}
    hit = by_cf.get(v.casefold())
    if hit is not None:
        return hit, None
    return None, f"value {value!r} not in allowed options: {options[:20]}"


def validate_field_values_detailed(
    field_values: dict[str, Any],
    normalized: dict[str, Any] | None,
    *,
    options_check: bool = False,
    board_id: str = "",
    schema_fetch_ok: bool | None = None,
    schema_fetch_message: str = "",
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate field keys (and optionally option values) against the board schema.

    Returns ``(cleaned_values, errors, warnings)``.

    Semantics:
      - Empty input -> ``({}, [], [])``.
      - Placeholder-pattern keys are rejected only when they are not present
        on this board schema (matches ``validate_field_values_against_board_schema``).
      - Unknown keys are rejected with a list of allowed keys.
      - When ``options_check`` is True and a field has options, the value is
        normalized to the canonical option label or rejected.
      - On any error, ``cleaned_values`` is empty so callers can do
        ``if errors: return early`` without leaking partial state.
      - When ``schema_fetch_ok`` is set and falsey, a warning is appended so
        the agent can mention that schema validation was best-effort.
    """
    del board_id  # accepted for symmetry with PR #10 callers; kept for future use
    warnings: list[str] = []
    if not field_values:
        return {}, [], warnings

    fields: list[Any] = []
    if isinstance(normalized, dict):
        raw = normalized.get("fields")
        if isinstance(raw, list):
            fields = raw

    allowed: set[str] = set()
    by_key: dict[str, dict[str, Any]] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        k = field_row_item_key(f)
        if k:
            allowed.add(k)
            by_key[k] = f

    errors: list[str] = []

    bad_ph = [
        k
        for k in field_values
        if looks_like_placeholder_plaky_field_key(str(k)) and str(k).strip() not in allowed
    ]
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

    if schema_fetch_ok is not None and not schema_fetch_ok:
        warnings.append(f"schema bundle returned warning: {schema_fetch_message or 'unknown'}")

    if not by_key:
        if not fields:
            warnings.append("board schema had no field definitions; skipped key/value validation")
        else:
            warnings.append("board schema had no field keys; skipped key/value validation")
        return dict(field_values), [], warnings

    cleaned: dict[str, Any] = {}
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


def _deep_find_field_lists(obj: Any, *, depth: int = 0, max_depth: int = 6) -> list[list[Any]]:
    """Nested board JSON sometimes nests field definition lists under settings/meta blocks."""
    if depth > max_depth or not isinstance(obj, dict):
        return []
    out: list[list[Any]] = []
    for k, v in obj.items():
        lk = str(k).lower()
        if isinstance(v, list) and v and isinstance(v[0], dict):
            sample = v[0]
            has_sig = any(
                sample.get(x)
                for x in (
                    "itemFieldKey",
                    "fieldKey",
                    "field",
                    "fieldName",
                    "boardFieldId",
                    "itemFieldId",
                )
            )
            if has_sig and any(
                t in lk
                for t in ("field", "column", "item", "board", "custom", "property", "definition")
            ):
                out.append(v)
        elif isinstance(v, dict):
            out.extend(_deep_find_field_lists(v, depth=depth + 1, max_depth=max_depth))
    return out


def field_stubs_from_board_items(
    items: list[dict[str, Any]], *, limit: int = 5
) -> list[dict[str, Any]]:
    """
    When GET board omits field definitions, infer field keys + labels from item payloads
    (timeline/list API usually embeds itemFields / fields per item).
    """
    stubs: list[dict[str, Any]] = []
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
    existing: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append schema field rows from `additions` when their `key` is not already present."""
    by_k: dict[str, dict[str, Any]] = {}
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


def normalize_board_payload(
    board_raw: dict[str, Any] | None, groups: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Extract board name, groups, and field definitions with option lists when present.
    Works across varying Plaky API shapes by scanning common keys.
    """
    board_name = ""
    fields: list[dict[str, Any]] = []
    if isinstance(board_raw, dict):
        board_name = str(board_raw.get("name") or board_raw.get("title") or "").strip()
        field_lists: list[Any] = []
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
    deduped: list[dict[str, Any]] = []
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
        "groups": [
            {"id": str(g.get("id", "")), "name": str(g.get("name", ""))}
            for g in groups
            if g.get("id")
        ],
        "fields": deduped,
    }


def format_board_schema_markdown(
    board_id: str,
    *,
    ok: bool,
    message: str = "",
    normalized: dict[str, Any] | None = None,
    raw_top_keys: list[str] | None = None,
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
        lines.append(
            "**Fields (use these labels/values when describing or updating items — match API literals):**"
        )
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
                    if lab:
                        opt_labels.append(lab)
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


async def fetch_board_schema_bundle(board_id: str) -> dict[str, Any]:
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

    if ttl > 0:
        from boardman.cache.agent_redis import agent_redis_get_json

        remote = await agent_redis_get_json(_schema_redis_key(bid))
        if isinstance(remote, dict) and remote.get("_boardman_schema_v1") is True:
            try:
                payload = remote.get("data")
                if isinstance(payload, str):
                    data = json.loads(payload)
                elif isinstance(payload, dict):
                    data = payload
                else:
                    data = None
                if isinstance(data, dict):
                    async with _schema_lock:
                        _schema_cache[bid] = (time.monotonic(), data)
                    return data
            except (json.JSONDecodeError, TypeError):
                pass

    c = PlakyClient()
    groups_r, board_r = await asyncio.gather(c.list_groups(bid), c.get_board(bid))
    groups = groups_r.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    board_raw = board_r.get("board") if board_r.get("ok") else None
    raw_keys: list[str] = []
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
                normalized["fields"] = merge_normalized_field_list(
                    normalized.get("fields") or [], stubs
                )
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
        from boardman.cache.agent_redis import agent_redis_set_json

        await agent_redis_set_json(
            _schema_redis_key(bid),
            {"_boardman_schema_v1": True, "data": json.dumps(result, default=str)},
            int(ttl),
        )
    return result
