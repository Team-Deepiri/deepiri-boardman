"""Pay the assistant's cold-start cost at boot instead of on the first question.

The first chat after a restart was measurably the slowest one: building its system
prompt fetches the board schema and assembles the team roster, and both of those are
network round trips whose caches are empty in a fresh process. Nothing about that work
depends on the question, so it can happen while the service is starting up.

Best effort by design. A warm-up that fails changes nothing except that the first
request pays what it used to pay.
"""

from __future__ import annotations

import asyncio
import logging
import time

_log = logging.getLogger(__name__)


def _board_ids() -> list[str]:
    """Every board the routing config can send work to, most-used first."""
    from boardman.repos_config import list_registered_repos, team_assignment_field_sync_board_id

    ids: list[str] = []
    primary = (team_assignment_field_sync_board_id() or "").strip()
    if primary:
        ids.append(primary)
    try:
        for routing in list_registered_repos().values():
            bid = str(getattr(routing, "plaky_board_id", "") or "").strip()
            if bid and bid not in ids:
                ids.append(bid)
    except Exception:  # noqa: BLE001 - Plaky API failure degrades gracefully
        pass
    return ids[:4]  # a handful of boards; this is a warm-up, not a crawl


async def warm_agent_caches() -> None:
    """Fill the caches the first assistant turn would otherwise fill itself."""
    started = time.monotonic()
    from boardman.assignment.config import load_team_assignments
    from boardman.plaky.board_schema import fetch_board_schema_bundle

    async def roster() -> str:
        # Assembling the roster makes a blocking, paginated Plaky call; keep it off the
        # event loop so it does not stall the requests that arrive during startup.
        await asyncio.to_thread(load_team_assignments)
        return "roster"

    jobs: list[asyncio.Future[str] | asyncio.Task[str]] = [asyncio.ensure_future(roster())]
    for bid in _board_ids():
        jobs.append(asyncio.ensure_future(fetch_board_schema_bundle(bid)))
    done = await asyncio.gather(*jobs, return_exceptions=True)
    failed = sum(1 for d in done if isinstance(d, BaseException))
    _log.info(
        "agent warm-up: %d/%d caches filled in %.1fs",
        len(done) - failed,
        len(done),
        time.monotonic() - started,
    )
