"""Per chat session: saved Plaky assignee + field defaults for the next create."""

from __future__ import annotations

import json
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from boardman.assignment.config import load_team_assignments
from boardman.database.models import AgentSession


def normalize_task_title(
    raw: str,
    *,
    mode: Literal["error", "truncate"] = "error",
    max_len: int = 160,
) -> tuple[str, str | None]:
    """
    Normalize a task title length.

    - ``mode="error"``: empty or over ``max_len`` returns ``("", error_message)``.
    - ``mode="truncate"``: over ``max_len`` is truncated; empty still errors.
    """
    t = (raw or "").strip()
    if not t:
        return "", "title must be non-empty"
    if len(t) <= max_len:
        return t, None
    if mode == "truncate":
        return t[:max_len], None
    return "", f"title must be <= {max_len} characters"


def _parse_draft(raw: str | None) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {"field_values": {}, "summary": ""}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {"field_values": {}, "summary": ""}
    if not isinstance(d, dict):
        return {"field_values": {}, "summary": ""}
    fv = d.get("field_values")
    if not isinstance(fv, dict):
        fv = {}
    summary = d.get("summary")
    return {
        "field_values": {str(k): str(v) if v is not None else "" for k, v in fv.items() if k},
        "summary": str(summary).strip() if summary else "",
    }


def _dump_draft(d: dict[str, Any]) -> str:
    out = {
        "field_values": dict(d.get("field_values") or {}),
        "summary": str(d.get("summary") or "").strip(),
    }
    return json.dumps(out, ensure_ascii=False)


async def load_task_draft(session: AsyncSession, agent_session_pk: int) -> dict[str, Any]:
    r = await session.execute(select(AgentSession).where(AgentSession.id == agent_session_pk))
    ag = r.scalar_one_or_none()
    if not ag:
        return {"field_values": {}, "summary": ""}
    return _parse_draft(ag.task_draft_json)


async def save_task_draft_merge(
    session: AsyncSession,
    agent_session_pk: int,
    *,
    field_values_patch: dict[str, Any] | None = None,
    engineer_plaky_id: str = "",
    qa_plaky_id: str = "",
    summary: str = "",
    replace_field_values: bool = False,
) -> dict[str, Any]:
    r = await session.execute(select(AgentSession).where(AgentSession.id == agent_session_pk))
    ag = r.scalar_one_or_none()
    if not ag:
        return {"ok": False, "message": "agent session not found"}

    cur = _parse_draft(ag.task_draft_json)
    fv: dict[str, str] = {}
    if replace_field_values:
        fv = {}
    else:
        fv = dict(cur["field_values"])

    cfg = load_team_assignments()
    eng = (engineer_plaky_id or "").strip()
    qa = (qa_plaky_id or "").strip()
    if eng and cfg.plaky_field_engineer:
        fv[cfg.plaky_field_engineer] = eng
    if qa and cfg.plaky_field_qa:
        fv[cfg.plaky_field_qa] = qa

    patch = field_values_patch or {}
    if isinstance(patch, dict):
        for k, v in patch.items():
            ks = str(k).strip()
            if not ks:
                continue
            if v is None or v == "":
                fv.pop(ks, None)
            else:
                fv[ks] = v if isinstance(v, str) else str(v)

    new_summary = summary.strip() if summary and summary.strip() else cur["summary"]
    if not new_summary and (eng or qa or patch):
        bits = []
        if eng:
            bits.append("engineer set")
        if qa:
            bits.append("QA set")
        if patch:
            bits.append(f"fields: {', '.join(sorted(str(k) for k in patch.keys()))}")
        new_summary = "; ".join(bits) if bits else ""

    ag.task_draft_json = _dump_draft({"field_values": fv, "summary": new_summary})
    await session.flush()
    return {"ok": True, "field_values": fv, "summary": new_summary}


def format_task_draft_for_prompt(draft: dict[str, Any]) -> str:
    fv = draft.get("field_values") or {}
    summary = draft.get("summary") or ""
    lines = [
        "",
        "## Saved task defaults (this chat session)",
        "These apply to **plaky_create_task** until changed — merged into `field_values_json` (explicit tool args override).",
    ]
    if summary:
        lines.append(f"**Summary:** {summary}")
    if not fv:
        lines.append(
            "**None yet.** Before creating a task, ask the user who should be the **engineer** and **QA** assignees "
            "(if your board uses them) and what values they want for **each field** listed in **Current Plaky board schema** "
            "(status, priority, type, etc.). Use **plaky_list_workspace_users** to resolve people by name → Plaky id. "
            "Then call **plaky_save_task_preferences** with JSON containing `field_values` and/or `engineer_plaky_id` / `qa_plaky_id`."
        )
    else:
        lines.append("**field_values (Plaky field key → value id or literal):**")
        for k, v in list(fv.items())[:40]:
            lines.append(f"- `{k}` → `{v}`")
        if len(fv) > 40:
            lines.append("- …")
        lines.append(
            "Update anytime with **plaky_save_task_preferences** (merges by default; set `replace_field_values` true to clear)."
        )
    return "\n".join(lines)


def merge_draft_into_field_values(
    draft: dict[str, Any],
    tool_field_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Draft base, then tool call overlays (tool wins)."""
    base = dict(draft.get("field_values") or {})
    over = tool_field_values if isinstance(tool_field_values, dict) else {}
    for k, v in over.items():
        ks = str(k).strip()
        if ks and v is not None and str(v).strip() != "":
            base[ks] = v if isinstance(v, str) else str(v)
    return base
