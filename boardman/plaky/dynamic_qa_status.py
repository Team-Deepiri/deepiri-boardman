"""
Map GitHub PR review outcomes to Plaky status **option UUIDs** using the live board schema.

No hardcoded Plaky labels: we score status option names against intent hints (e.g. approve →
"QA Verified", request changes → "QA Rejected"). Optional env/settings still override when set.

Also: discover QA assignee field key from schema, and resolve GitHub actor → Plaky workspace user id
via exact linked GitHub handle on the Plaky user row when present, then the same fuzzy
``best_plaky_match_for_github`` pipeline used by assignment / sync (email, name, login heuristics).
"""

from __future__ import annotations

import logging
from typing import Any

from boardman.assignment.identity_match import best_plaky_match_for_github
from boardman.plaky.board_schema import fetch_board_schema_bundle
from boardman.plaky.client import PlakyClient
from boardman.services.sync_state import UNREADABLE_STATUS as _SYNC_STATE_UNREADABLE
from boardman.settings import settings

_log = logging.getLogger(__name__)

# (hint phrases / words — normalized with underscores as spaces), negative substrings penalize.
_GITHUB_APPROVE_HINTS: tuple[str, ...] = (
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
_GITHUB_APPROVE_NEG: tuple[str, ...] = (
    "reject",
    "changes",
    "pending approval",
    "awaiting approval",
    "not approved",
    "blocked",
    "on hold",
    "draft",
)
_GITHUB_CHANGES_HINTS: tuple[str, ...] = (
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
_WORKFLOW_IN_QA_HINTS: tuple[str, ...] = (
    "in qa",
    "qa in progress",
    "under qa",
    "qa testing",
    "in test",
    "testing",
)
_WORKFLOW_NEEDS_QA_HINTS: tuple[str, ...] = (
    "needs qa",
    "awaiting qa",
    "ready for qa",
    "todo qa",
    "qa todo",
    "queue qa",
)
_WORKFLOW_NEEDS_QA_AGAIN_HINTS: tuple[str, ...] = (
    "needs qa again",
    "qa again",
    "re qa",
    "back to qa",
)
# PR matched a task but no one is assigned yet.
_WORKFLOW_NEEDS_ASSIGNED_HINTS: tuple[str, ...] = (
    "needs assigned",
    "need assigned",
    "needs assignee",
    "unassigned",
    "available",
    "backlog",
    "to do",
    "todo",
    "open",
)
_WORKFLOW_NEEDS_ASSIGNED_NEG: tuple[str, ...] = (
    "in progress",
    "qa",
    "done",
    "completed",
    "paused",
    "verified",
    "rejected",
)
# A developer has been matched/assigned to the task.
_WORKFLOW_ASSIGNED_HINTS: tuple[str, ...] = (
    "assigned",
    "claimed",
    "accepted",
    "picked up",
)
_WORKFLOW_ASSIGNED_NEG: tuple[str, ...] = (
    "needs assigned",
    "need assigned",
    "unassigned",
    "needs qa",
    "in qa",
    "qa verified",
    "qa rejected",
    "completed",
    "done",
    "paused",
)
# Work is paused / on hold (from a PR comment saying "pause"/"paused").
_WORKFLOW_PAUSED_HINTS: tuple[str, ...] = (
    "paused",
    "pause",
    "on hold",
    "taking a break",
    "blocked",
    "stuck",
)
_WORKFLOW_PAUSED_NEG: tuple[str, ...] = (
    "qa",
    "done",
    "completed",
)
# Active development (e.g. dev resumed work after a QA rejection).
_WORKFLOW_IN_PROGRESS_HINTS: tuple[str, ...] = (
    "in progress",
    "revisions in progress",
    "doing",
    "wip",
    "active",
    "working",
    "in development",
)
_WORKFLOW_IN_PROGRESS_NEG: tuple[str, ...] = (
    "needs",
    "qa",
    "done",
    "completed",
    "paused",
    "available",
)
# Merged → done.
_WORKFLOW_COMPLETED_HINTS: tuple[str, ...] = (
    "completed",
    "complete",
    "done",
    "merged",
    "shipped",
    "finished",
    "resolved",
    "closed",
)
_WORKFLOW_COMPLETED_NEG: tuple[str, ...] = (
    "incomplete",
    "not done",
    "in progress",
    "paused",
    "qa",
)


def _norm(s: str) -> str:
    return " ".join(s.strip().lower().replace("_", " ").replace("-", " ").split())


def _score_option_label(
    label_norm: str, hints: tuple[str, ...], negative: tuple[str, ...]
) -> float:
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


def _status_fields(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in normalized.get("fields") or []:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "").lower()
        fname = (f.get("name") or "").lower()
        if "status" in ftype or "status" in fname:
            out.append(f)
    return out


def _pick_best_option(
    fields: list[dict[str, Any]],
    hints: tuple[str, ...],
    negative: tuple[str, ...],
    *,
    require_substring: str = "",
) -> tuple[str, str, float]:
    """Return (field_key, option_id, score).

    ``require_substring``: when set, only options whose normalized label contains it are
    considered (e.g. "again" — so a board without a dedicated column yields no match and the
    caller can fall back to a base intent).
    """
    req = _norm(require_substring) if require_substring else ""
    best: tuple[str, str, float] = ("", "", float("-inf"))
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
            if req and req not in ln:
                continue
            sc = _score_option_label(ln, hints, negative)
            if sc > best[2]:
                best = (fkey, oid, sc)
    return best


async def _load_normalized(board_id: str) -> dict[str, Any] | None:
    """The board's schema, or None when it cannot be had right now.

    None covers a transport failure as well as a board that answered unusably. The fetch
    is a network call and `get_board` re-raises what httpx raised, while every caller here
    is a resolver or a guard reached from ordinary webhook handling: letting the exception
    out fails the whole event and leaves the board half-written, when the honest answer is
    "I could not tell".
    """
    from boardman.observability.degradation import log_degraded

    bid = (board_id or "").strip()
    if not bid:
        return None
    try:
        bundle = await fetch_board_schema_bundle(bid)
    except Exception as exc:  # noqa: BLE001 - a resolver must not break the sync it serves
        log_degraded(_log, f"loading board {bid} schema", exc)
        return None
    if not bundle.get("ok") or not bundle.get("normalized"):
        return None
    n = bundle["normalized"]
    return n if isinstance(n, dict) else None


async def resolve_plaky_status_patch(
    board_id: str,
    *,
    intent: str,
    preloaded_normalized: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """
    Return ``(status_field_key, option_id)`` for a workflow intent, or None.

    Intents:
      - ``github_pr_review_approved`` — GitHub review **Approve** → QA verified-style column.
      - ``github_pr_review_changes_requested`` — **Request changes** → QA rejected-style.
      - ``workflow_in_qa`` — move into active QA.
      - ``workflow_needs_qa`` — awaiting QA.
      - ``workflow_needs_qa_again`` — dev pinged QA again after rework.
      - ``workflow_needs_assigned`` — PR matched a task but no assignee yet.
      - ``workflow_assigned`` — a developer has been matched/assigned.
      - ``workflow_paused`` — work paused / on hold (PR comment said pause).
      - ``workflow_in_progress`` — active development (e.g. resumed after QA rejection).
      - ``workflow_completed`` — merged / done.
    """
    normalized = preloaded_normalized
    if normalized is None:
        normalized = await _load_normalized(board_id)
    if not normalized:
        return None

    fields = _status_fields(normalized)
    if not fields:
        return None

    require_substring = ""
    if intent == "github_pr_review_approved":
        hints, neg = _GITHUB_APPROVE_HINTS, _GITHUB_APPROVE_NEG
    elif intent == "github_pr_review_changes_requested":
        hints, neg = _GITHUB_CHANGES_HINTS, ()
    elif intent == "workflow_in_qa":
        hints, neg = _WORKFLOW_IN_QA_HINTS, ()
    elif intent == "workflow_needs_qa":
        hints, neg = _WORKFLOW_NEEDS_QA_HINTS, ()
    elif intent == "workflow_needs_qa_again":
        # Only matches a dedicated "...AGAIN" column; otherwise the caller falls back to
        # workflow_needs_qa. Returning to Needs QA is the INTENDED behavior (Ali:
        # "needs QA again means it just goes back to Needs QA"), not a degradation —
        # a separate column is optional and no board is expected to define one.
        hints, neg, require_substring = _WORKFLOW_NEEDS_QA_AGAIN_HINTS, (), "again"
    elif intent == "workflow_needs_assigned":
        hints, neg = _WORKFLOW_NEEDS_ASSIGNED_HINTS, _WORKFLOW_NEEDS_ASSIGNED_NEG
    elif intent == "workflow_assigned":
        hints, neg = _WORKFLOW_ASSIGNED_HINTS, _WORKFLOW_ASSIGNED_NEG
    elif intent == "workflow_paused":
        hints, neg = _WORKFLOW_PAUSED_HINTS, _WORKFLOW_PAUSED_NEG
    elif intent == "workflow_in_progress":
        hints, neg = _WORKFLOW_IN_PROGRESS_HINTS, _WORKFLOW_IN_PROGRESS_NEG
    elif intent == "workflow_completed":
        hints, neg = _WORKFLOW_COMPLETED_HINTS, _WORKFLOW_COMPLETED_NEG
    else:
        return None

    fk, oid, sc = _pick_best_option(fields, hints, neg, require_substring=require_substring)
    if not fk or not oid or sc <= 0:
        return None
    return (fk, oid)


def discover_qa_assignee_field_key_from_normalized(normalized: dict[str, Any]) -> str | None:
    """
    Best-effort QA person field: schema field whose name/key suggests QA and type suggests a person.
    """
    best: tuple[int, str] = (-10_000, "")
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


async def discover_qa_assignee_field_key(board_id: str) -> str | None:
    n = await _load_normalized(board_id)
    if not n:
        return None
    return discover_qa_assignee_field_key_from_normalized(n)


def _github_actor_dict(login: str, *, name: str = "", email: str = "") -> dict[str, Any]:
    return {
        "login": (login or "").strip(),
        "name": (name or "").strip(),
        "email": (email or "").strip(),
    }


def github_actor_payload(user: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize GitHub ``user`` objects from webhooks into the shape expected by identity matching."""
    if not isinstance(user, dict):
        return _github_actor_dict("")
    return _github_actor_dict(
        str(user.get("login") or ""),
        name=str(user.get("name") or ""),
        email=str(user.get("email") or ""),
    )


async def resolve_github_user_to_plaky_user_id(
    gh: dict[str, Any],
    *,
    min_score: int = 640,
    ambiguity_margin: int = 45,
) -> str | None:
    """
    Map a GitHub profile (login, optional name/email from webhook) to a Plaky workspace user id.

    1) Prefer an explicit GitHub username stored on the Plaky user (exact case-insensitive match).
    2) Otherwise run ``best_plaky_match_for_github`` (email / display name / login-token heuristics;
       conservative ambiguity handling — same as ``sync_qa_capabilities`` roster matching).
    """
    login = str(gh.get("login") or "").strip()
    if not login:
        return None
    want = login.casefold()

    # Explicit roster mapping wins: team_assignments member_overrides exists precisely
    # for accounts the fuzzy matcher cannot bridge (login and Plaky email share nothing).
    try:
        from boardman.assignment.config import load_team_assignments

        for m in load_team_assignments().members:
            gl = (getattr(m, "github_login", "") or "").strip().casefold()
            mid = (getattr(m, "id", "") or "").strip()
            if gl and mid and gl == want:
                return mid
    except Exception:  # noqa: BLE001 — roster trouble must never break identity resolution
        _log.debug("roster unavailable during GitHub user resolution", exc_info=True)

    c = PlakyClient()
    r = await c.list_workspace_users()
    if not r.get("ok"):
        return None
    users: list[dict[str, Any]] = [u for u in (r.get("users") or []) if isinstance(u, dict)]
    for u in users:
        uid = str(u.get("id") or "").strip()
        if not uid:
            continue
        linked = u.get("github_login") or u.get("githubLogin") or u.get("githubUsername")
        if isinstance(linked, str) and linked.strip().casefold() == want:
            return uid

    plaky_id, reason, _ = best_plaky_match_for_github(
        gh,
        users,
        min_score=min_score,
        ambiguity_margin=ambiguity_margin,
    )
    if reason == "matched" and plaky_id:
        return str(plaky_id).strip() or None
    return None


async def workspace_plaky_user_id_for_github_login(login: str) -> str | None:
    """Backward-compatible: login-only GitHub handle → Plaky id (exact link row, then fuzzy)."""
    return await resolve_github_user_to_plaky_user_id(_github_actor_dict(login))


def configured_qa_item_field_key() -> str:
    """Env/settings override for Plaky item field key holding QA assignee (before team_assignments.yml)."""
    return (getattr(settings, "plaky_qa_item_field_key", "") or "").strip()


async def resolve_qa_assignee_field_key(board_id: str, yaml_fallback: str) -> str:
    """QA assignee field key: env override → live board schema → team_assignments fallback.

    Schema beats the YAML key because category boards use different person-field keys
    (e.g. QA is person-3 on one board, person-4 on another); the global YAML key can name
    the wrong column. Falls back to YAML only when the schema is unavailable.
    """
    k = configured_qa_item_field_key()
    if k:
        return k
    discovered = await discover_qa_assignee_field_key(board_id)
    if discovered:
        return discovered
    return (yaml_fallback or "").strip()


# Returned when the board could not be read at all. Distinct from "" (read fine, but the
# option matches no intent this code knows), because the two demand opposite behaviour:
# an unknown LABEL is not evidence of anything, while an unknown BOARD means the guard
# simply did not run and a status write would be taken on faith.
# Imported rather than repeated: two copies that drifted would leave both fail-closed
# guards permitting exactly the writes they exist to refuse, with nothing failing.
UNREADABLE_STATUS = _SYNC_STATE_UNREADABLE


async def workflow_status_field_key(board_id: str) -> str | None:
    """The board's workflow status column, for a caller that needs one to read a task.

    Three answers, and they are not the same thing. A key: read the task there. `""`: the
    schema loaded and none of this code's vocabulary matched any column, so there is no
    workflow position to protect and nothing to be careful about. `None`: the board did
    not answer at all, which is the case a guard must fail closed on.
    """
    bid = (board_id or "").strip()
    if not bid:
        return ""
    normalized = await _load_normalized(bid)
    if not normalized:
        return None
    from boardman.services.sync_state import WORKFLOW_RANK

    for intent in WORKFLOW_RANK:
        resolved = await resolve_plaky_status_patch(
            bid, intent=intent, preloaded_normalized=normalized
        )
        if resolved and resolved[0]:
            return str(resolved[0])
    return ""


async def current_status_intent(board_id: str, task_id: str, status_field_key: str) -> str:
    """Which workflow intent the task's CURRENT status option corresponds to, "" if unknown.

    The inverse of `resolve_plaky_status_patch`: that turns an intent into this board's
    option id, and this turns an option id back into the intent it came from. Boards name
    their columns differently, so the only honest way to ask "how far along is this task"
    is to resolve every intent against the board and see which one the task is sitting on.

    Every lookup rides the board-schema cache, so this costs one item read and no extra
    Plaky calls. Returns "" for an option no intent claims -- a board using vocabulary
    this code cannot place is not evidence of anything.
    """
    from boardman.observability.degradation import log_degraded
    from boardman.plaky.board_schema import plaky_item_status_id
    from boardman.plaky.client import PlakyClient
    from boardman.services.sync_state import WORKFLOW_RANK, workflow_rank

    bid = (board_id or "").strip()
    fk = (status_field_key or "").strip()
    if not bid or not fk or not str(task_id or "").strip():
        return ""
    try:
        info = await PlakyClient().get_board_item_public(bid, str(task_id))
    except Exception as exc:  # noqa: BLE001 - a guard must not break the sync it guards
        # The read is new: these callers made no item read before the guard existed, so a
        # Plaky transport blip must not start aborting whole issue and PR syncs.
        log_degraded(_log, f"current_status_intent: reading task {task_id}", exc)
        return UNREADABLE_STATUS
    if not info.get("ok") or not info.get("item"):
        # Distinct from "": the board did not answer, so the caller knows the guard could
        # not run rather than being told the task is nowhere in particular.
        _log.warning(
            "could not read task %s status on board %s; workflow position unknown",
            task_id,
            bid,
        )
        return UNREADABLE_STATUS
    current = plaky_item_status_id(info["item"], fk)
    if not current:
        return ""

    # One schema load for all ten intents. Resolving each separately was twenty Plaky
    # calls per guard with the schema cache disabled, and a transient failure on any one
    # of them came back as "" -- read as "no regression", which silently defeats the
    # guard. A load we cannot do is UNREADABLE, and refuses the write.
    normalized = await _load_normalized(bid)
    if not normalized:
        _log.warning("could not load board %s schema; workflow position unknown", bid)
        return UNREADABLE_STATUS

    best = ""
    best_rank: int | None = None
    for intent in WORKFLOW_RANK:
        resolved = await resolve_plaky_status_patch(
            bid, intent=intent, preloaded_normalized=normalized
        )
        # Compare the FIELD as well as the option: Plaky types Type and Priority as STATUS
        # columns and their option ids restart per field, so "3" on Priority would
        # otherwise read as "3" on Status and hold back a legitimate write.
        if not resolved or str(resolved[0] or "") != fk or str(resolved[1]) != str(current):
            continue
        rank = workflow_rank(intent)
        # Several intents can share one option (a board with no "Needs QA Again" column
        # maps both there). Take the LOWEST rank of the matches: the guard should refuse
        # a write only when it is certain the task is further along, never on a guess.
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = intent, rank
    return best
