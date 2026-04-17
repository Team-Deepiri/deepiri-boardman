"""
Map GitHub PR review outcomes to Plaky status **option UUIDs** using the live board schema.

No hardcoded Plaky labels: we score status option names against intent hints (e.g. approve →
"QA Verified", request changes → "QA Rejected"). Optional env/settings still override when set.

Also: discover QA assignee field key from schema, and match GitHub login → Plaky user id from
workspace users (when Plaky returns a GitHub username field).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.settings import settings

# (hint phrases / words — normalized with underscores as spaces), negative substrings penalize.
_GITHUB_APPROVE_HINTS: Tuple[str, ...] = (
    "qa verified",
    "verified",
    "qa passed",
    "passed qa",
    "qa complete",
    "signed off",
    "sign off",
    "qa signoff",
    "approval",
    "approved for merge",
    "merge approved",
)
_GITHUB_APPROVE_NEG: Tuple[str, ...] = (
    "reject",
    "changes",
    "pending approval",
    "awaiting approval",
    "not approved",
    "blocked",
    "on hold",
    "draft",
)
_GITHUB_CHANGES_HINTS: Tuple[str, ...] = (
    "qa rejected",
    "rejected",
    "changes requested",
    "request changes",
    "needs rework",
    "revision",
    "send back",
    "failed qa",
    "needs fix",
)
_WORKFLOW_IN_QA_HINTS: Tuple[str, ...] = (
    "in qa",
    "qa in progress",
    "under qa",
    "qa testing",
    "in test",
    "testing",
)
_WORKFLOW_NEEDS_QA_HINTS: Tuple[str, ...] = (
    "needs qa",
    "awaiting qa",
    "ready for qa",
    "todo qa",
    "qa todo",
    "queue qa",
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _norm(s: str) -> str:
    return " ".join(s.strip().lower().replace("_", " ").replace("-", " ").split())


def _score_option_label(label_norm: str, hints: Tuple[str, ...], negative: Tuple[str, ...]) -> float:
    score = 0.0
    for neg in negative:
        if neg in label_norm:
            score -= 12.0
    for h in hints:
        hn = _norm(h)
        if not hn:
            continue
        if hn in label_norm:
            score += min(40.0, len(hn) * 2.2)
        for w in hn.split():
            if len(w) >= 3 and w in label_norm.split():
                score += 1.5
    return score


def _status_fields(normalized: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for f in normalized.get("fields") or []:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "").lower()
        fname = (f.get("name") or "").lower()
        if "status" in ftype or "status" in fname:
            out.append(f)
    return out


def _pick_best_option(
    fields: List[Dict[str, Any]],
    hints: Tuple[str, ...],
    negative: Tuple[str, ...],
) -> Tuple[str, str, float]:
    """Return (field_key, option_id, score)."""
    best: Tuple[str, str, float] = ("", "", float("-inf"))
    for f in fields:
        fkey = str(f.get("key") or "").strip()
        if not fkey:
            continue
        for opt in f.get("options") or []:
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("name") or opt.get("label") or opt.get("title") or "").strip()
            oid = str(opt.get("id") or opt.get("value") or "").strip()
            if not label or not oid:
                continue
            ln = _norm(label)
            sc = _score_option_label(ln, hints, negative)
            if sc > best[2]:
                best = (fkey, oid, sc)
    return best


async def _load_normalized(board_id: str) -> Optional[Dict[str, Any]]:
    bid = (board_id or "").strip()
    if not bid:
        return None
    bundle = await fetch_board_schema_bundle(bid)
    if not bundle.get("ok") or not bundle.get("normalized"):
        return None
    n = bundle["normalized"]
    return n if isinstance(n, dict) else None


async def resolve_plaky_status_patch(
    board_id: str,
    *,
    intent: str,
    preloaded_normalized: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """
    Return ``(status_field_key, option_id)`` for a workflow intent, or None.

    Intents:
      - ``github_pr_review_approved`` — GitHub review **Approve** → QA verified-style column.
      - ``github_pr_review_changes_requested`` — **Request changes** → QA rejected-style.
      - ``workflow_in_qa`` — move into active QA.
      - ``workflow_needs_qa`` — awaiting QA.
    """
    normalized = preloaded_normalized
    if normalized is None:
        normalized = await _load_normalized(board_id)
    if not normalized:
        return None

    fields = _status_fields(normalized)
    if not fields:
        return None

    if intent == "github_pr_review_approved":
        hints, neg = _GITHUB_APPROVE_HINTS, _GITHUB_APPROVE_NEG
    elif intent == "github_pr_review_changes_requested":
        hints, neg = _GITHUB_CHANGES_HINTS, ()
    elif intent == "workflow_in_qa":
        hints, neg = _WORKFLOW_IN_QA_HINTS, ()
    elif intent == "workflow_needs_qa":
        hints, neg = _WORKFLOW_NEEDS_QA_HINTS, ()
    else:
        return None

    fk, oid, sc = _pick_best_option(fields, hints, neg)
    if not fk or not oid or sc <= 0:
        return None
    return (fk, oid)


def discover_qa_assignee_field_key_from_normalized(normalized: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort QA person field: schema field whose name/key suggests QA and type suggests a person.
    """
    best: Tuple[int, str] = (-10_000, "")
    for f in normalized.get("fields") or []:
        if not isinstance(f, dict):
            continue
        name = (f.get("name") or "").strip()
        key = str(f.get("key") or "").strip()
        ftype = (f.get("type") or "").lower()
        nl = name.lower()
        kl = key.lower()
        if "qa" not in nl and "qa" not in kl:
            continue
        if "status" in nl and "person" not in ftype and "user" not in ftype:
            # "QA status" column — skip unless clearly a person field
            if "person" not in ftype and "user" not in ftype and "member" not in ftype:
                continue
        score = 0
        if "person" in ftype or "user" in ftype or "member" in ftype or "people" in ftype:
            score += 50
        if "select" in ftype and ("user" in nl or "assign" in nl):
            score += 40
        if "qa" in nl:
            score += 20
        if score > best[0] and key:
            best = (score, key)
    return best[1] or None


async def discover_qa_assignee_field_key(board_id: str) -> Optional[str]:
    n = await _load_normalized(board_id)
    if not n:
        return None
    return discover_qa_assignee_field_key_from_normalized(n)


async def workspace_plaky_user_id_for_github_login(login: str) -> Optional[str]:
    """Match Plaky workspace user to GitHub login when API exposes a GitHub username."""
    raw = (login or "").strip()
    if not raw:
        return None
    want = raw.casefold()
    c = PlakyClient()
    r = await c.list_workspace_users()
    if not r.get("ok"):
        return None
    for u in r.get("users") or []:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "").strip()
        gh = u.get("github_login") or u.get("githubLogin") or u.get("githubUsername")
        if isinstance(gh, str) and gh.strip().casefold() == want:
            return uid or None
    return None


def configured_qa_item_field_key() -> str:
    """Env/settings override for Plaky item field key holding QA assignee (before team_assignments.yml)."""
    return (getattr(settings, "plaky_qa_item_field_key", "") or "").strip()


async def resolve_qa_assignee_field_key(board_id: str, yaml_fallback: str) -> str:
    """Prefer env, then team_assignments key, then schema discovery."""
    k = configured_qa_item_field_key()
    if k:
        return k
    y = (yaml_fallback or "").strip()
    if y:
        return y
    return (await discover_qa_assignee_field_key(board_id)) or ""


def looks_like_plaky_option_uuid(value: str) -> bool:
    return bool(_UUID_RE.match((value or "").strip()))
