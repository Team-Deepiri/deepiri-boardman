"""Resolve plain names to ClickUp workspace members.

Shared by the agent tools and anything else that takes a person as free text. An ambiguous or
unknown name is reported back as a problem string and never guessed.
"""

from __future__ import annotations

from typing import Any

from boardman.clickup.client import ClickUpClient
from boardman.name_match import rank_rows_by_name
from boardman.settings import settings


async def workspace_users(client: ClickUpClient) -> list[dict[str, Any]]:
    r = await client.list_workspace_users()
    return r.get("users") or [] if r.get("ok") else []


def rank_users(
    query: str, users: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Rank members by how well name and email match ``query`` (``(ranked, best)``)."""
    rows = [
        {"id": u["id"], "name": f"{u.get('name') or ''} {u.get('email') or ''}".strip()}
        for u in users
    ]
    return rank_rows_by_name(rows, (query or "").strip())


def match_person(
    query: str,
    users: list[dict[str, Any]],
    *,
    users_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve a plain name or email to one member. Returns ``(user, problem)``.

    Pass ``users_by_id`` when resolving several names against the same list to skip the lookup scan.
    """
    q = (query or "").strip()
    if not q:
        return None, ""
    ranked, best = rank_users(q, users)
    floor = settings.clickup_person_match_min_score
    strong = [r for r in ranked if r["score"] >= floor]
    if len(strong) > 1 and strong[0]["score"] == strong[1]["score"]:
        names = ", ".join(r["name"] for r in strong[:4])
        return None, f"'{q}' is ambiguous: {names}"
    if not best or best["score"] < floor:
        return None, f"no workspace member matches '{q}'"
    if users_by_id is not None:
        return users_by_id.get(best["id"]), ""
    return next((u for u in users if u["id"] == best["id"]), None), ""


def assignee_ids(user: dict[str, Any] | None) -> list[int] | None:
    """ClickUp wants integer user ids in assignee lists."""
    uid = str((user or {}).get("id", ""))
    return [int(uid)] if uid.isdigit() else None


async def resolve_people(
    client: ClickUpClient, assignee: str, qa: str
) -> tuple[list[int] | None, str | None, dict[str, str]]:
    """Resolve plain names to ``(assignee ids to add, QA user id, problems by role)``.

    Both ids are ``None`` when nothing was resolved (not asked for, or a problem was reported).
    """
    wanted = {"assignee": (assignee or "").strip(), "qa": (qa or "").strip()}
    if not any(wanted.values()):
        return None, None, {}
    users = await workspace_users(client)
    by_id = {u["id"]: u for u in users}
    problems: dict[str, str] = {}
    resolved: dict[str, dict[str, Any] | None] = {}
    for role, name in wanted.items():
        if not name:
            continue
        person, problem = match_person(name, users, users_by_id=by_id)
        resolved[role] = person
        if problem:
            problems[role] = problem
    qa_person = resolved.get("qa")
    return (
        assignee_ids(resolved.get("assignee")),
        (str(qa_person["id"]) if qa_person else None),
        problems,
    )
