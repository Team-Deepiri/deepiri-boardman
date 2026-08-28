"""Who may be written into the DEVELOPER (Assignee) column.

QA selection has had a real eligibility chain for a long time — role gate, exclusion
list, self-review guard, tier and repo rules. The developer column had none: anyone the
identity matcher resolved could land there, including a QA-only reviewer or an IT/
support account, and the assistant could put a name there just by being asked to.

This is the gate for that, and it is deliberately code rather than prompt text: an LLM
cannot talk its way past a filter it never sees. Every path that writes the engineer
column runs through `filter_developer`, so the webhook, the reconciler and the
assistant all obey the same rule.

Cheap by construction: pure in-memory checks against the already-cached roster, no
network, no per-call I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Roles that mean "this person writes the code".
DEVELOPER_ROLES = frozenset({"engineer", "developer", "dev", "contributor", "maintainer"})
# Roles that never do, on their own.
NON_DEVELOPER_ROLES = frozenset(
    {"qa", "quality", "tester", "test", "it", "support", "helpdesk", "ops-support", "intern-qa"}
)


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str


def _roles(member: Any) -> set[str]:
    return {
        str(r).strip().casefold() for r in (getattr(member, "roles", None) or []) if str(r).strip()
    }


def _label(member: Any) -> str:
    return (
        str(getattr(member, "display", "") or "").strip()
        or str(getattr(member, "github_login", "") or "").strip()
        or str(getattr(member, "id", "") or "").strip()
    )


def developer_eligibility(member: Any, cfg: Any = None) -> Eligibility:
    """Whether `member` may be assigned as the developer on a task."""
    if member is None:
        return Eligibility(False, "no such person on the roster")

    name = _label(member)
    if getattr(member, "active", True) is False:
        return Eligibility(False, f"{name} is not active")

    excluded = {
        " ".join(str(x).split()).casefold()
        for x in (getattr(cfg, "developer_excluded", None) or [])
        if str(x).strip()
    }
    if excluded:
        display = " ".join(str(getattr(member, "display", "") or "").split()).casefold()
        login = str(getattr(member, "github_login", "") or "").strip().casefold()
        if display in excluded or (login and login in excluded):
            return Eligibility(False, f"{name} is on the developer_excluded list")

    roles = _roles(member)
    if not roles:
        # No roles declared at all: the roster default makes everyone an engineer, so
        # silence is not a reason to refuse.
        return Eligibility(True, f"{name} has no role restrictions")
    if roles & DEVELOPER_ROLES:
        return Eligibility(True, f"{name} has a developer role")
    if roles & NON_DEVELOPER_ROLES:
        blocking = ", ".join(sorted(roles & NON_DEVELOPER_ROLES))
        return Eligibility(False, f"{name} is {blocking}-only, not a developer")
    return Eligibility(False, f"{name} has no developer role ({', '.join(sorted(roles))})")


def member_by_plaky_id(cfg: Any, plaky_id: str) -> Any | None:
    want = str(plaky_id or "").strip()
    if not want or cfg is None:
        return None
    pools = list(getattr(cfg, "members", None) or []) + list(
        getattr(cfg, "fallback_members", None) or []
    )
    for m in pools:
        if str(getattr(m, "id", "") or "").strip() == want:
            return m
    return None


def filter_developer(plaky_id: str, cfg: Any = None) -> tuple[str, str]:
    """(id_to_write, note). Returns ("", reason) when the person may not be a developer.

    A person the roster does not know is allowed through: the roster is the QA team, not
    the whole company, and refusing every unknown id would break assigning contractors
    and bot-adjacent accounts that legitimately own work.
    """
    pid = str(plaky_id or "").strip()
    if not pid:
        return "", ""
    if cfg is None:
        from boardman.assignment.config import load_team_assignments

        cfg = load_team_assignments()
    member = member_by_plaky_id(cfg, pid)
    if member is None:
        return pid, ""
    verdict = developer_eligibility(member, cfg)
    if verdict.ok:
        return pid, ""
    logger.info("developer assignment refused: %s", verdict.reason)
    return "", verdict.reason
