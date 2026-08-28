"""Resolve near-miss field values against a board's real option list.

The automation already does this: `Feature` is not an option on board 269031, so
`type_field_patch_candidates` walks Feature -> story -> task -> enhancement until one
matches. The assistant's create path skipped that ladder and rejected the value outright,
so "make me 5 tasks" failed on a word the same service accepts everywhere else.

Rejection is expensive in a way a plain API is not: the model has to guess WHY from an
error, and a wrong guess becomes a confident explanation to the user ("Plaky requires text,
not numbers" — it does not) followed by a question instead of a retry. Anything resolvable
should resolve here, and whatever cannot must fail with the exact allowed options.
"""

from __future__ import annotations

from typing import Any

from boardman.plaky.task_tag_vocab import (
    canonical_task_priority,
    canonical_task_status,
    canonical_task_type,
    priority_field_patch_candidates,
    status_field_patch_candidates,
    type_field_patch_candidates,
)

# Which ladder to try, by the column's human title.
_INTENT_BY_TITLE = (
    ("priority", (canonical_task_priority, priority_field_patch_candidates)),
    ("type", (canonical_task_type, type_field_patch_candidates)),
    ("status", (canonical_task_status, status_field_patch_candidates)),
)


def _field_rows(normalized: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(normalized, dict):
        return []
    rows = normalized.get("fields")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _options(row: dict[str, Any]) -> list[dict[str, Any]]:
    opts = row.get("options")
    return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []


def _match_option(row: dict[str, Any], wanted: str) -> str | None:
    """Board option LABEL whose id or label equals ``wanted``, case-insensitively.

    The label is what downstream validation checks against, so an option id ("0") must
    come back out as "NEEDS ASSIGNED" — returning the id here made a correct value look
    invalid.
    """
    want = str(wanted).strip().casefold()
    if not want:
        return None
    for opt in _options(row):
        oid = str(opt.get("id") or opt.get("key") or opt.get("value") or "").strip()
        label = str(opt.get("name") or opt.get("title") or opt.get("label") or "").strip()
        if oid.casefold() == want or label.casefold() == want:
            return label or oid
    return None


def coerce_field_values(
    field_values: dict[str, Any],
    normalized: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Map each value onto an option the board actually has.

    Returns ``(coerced, notes)``. Values already valid pass through untouched; values that
    resolve through a synonym ladder are rewritten and explained in ``notes``; values that
    resolve to nothing are left alone so the validator can reject them with real options.
    """
    rows = {str(r.get("key") or ""): r for r in _field_rows(normalized)}
    out: dict[str, Any] = {}
    notes: list[str] = []

    for key, value in (field_values or {}).items():
        row = rows.get(str(key))
        if row is None or not _options(row):
            out[key] = value
            continue

        direct = _match_option(row, value)
        if direct is not None:
            out[key] = direct
            continue

        title = str(row.get("name") or row.get("title") or row.get("label") or "").casefold()
        resolved: str | None = None
        for needle, (canonical, candidates) in _INTENT_BY_TITLE:
            if needle not in title:
                continue
            # default="" distinguishes a real synonym from the ladder's fallback. Without
            # it every unrecognized word canonicalizes to the default and "Backlog" would
            # quietly become "In Progress" — a wrong state on the board, written
            # confidently, which is worse than refusing the value.
            canon = canonical(str(value), default="")
            if not canon:
                break
            for candidate in candidates(canon):
                resolved = _match_option(row, candidate)
                if resolved is not None:
                    break
            break

        if resolved is None:
            out[key] = value  # let the validator report the allowed options
            continue

        out[key] = resolved
        notes.append(
            f"{key}: this board has no {value!r} option; used {resolved!r} "
            f"(the same mapping the GitHub automation applies)"
        )

    return out, notes
