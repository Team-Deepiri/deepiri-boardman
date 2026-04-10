"""GitHub org team roster (support team) for names / logins — not Plaky IDs."""

from __future__ import annotations

from fastapi import APIRouter

from boardman.github.team_roster import fetch_support_team_members

router = APIRouter()


@router.get("/github/support-team/members")
async def github_support_team_members() -> dict:
    """
    Members of `GITHUB_SUPPORT_TEAM` (default `Team-Deepiri/support-team`).

    Use logins + names to line up rows in `team_assignments.yml` with Plaky person ids
    from `GET /api/v1/plaky/users`. Assignment still uses Plaky `id` values in YAML.
    """
    return await fetch_support_team_members()
