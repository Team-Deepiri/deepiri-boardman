"""The roster must never freeze the event loop.

Assembling team_assignments makes a blocking, paginated Plaky call. Nineteen async call
sites reach it, so on an expired memo any one of them stalls every concurrent chat stream
and webhook in the process. Raised by a Sorge review on PR #88.

The roster changes when somebody joins the team. A two-minute-old copy is a correct
answer; a frozen event loop is not.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from boardman.assignment import config as cfg_mod


@pytest.fixture(autouse=True)
def _clean():
    cfg_mod.reload_team_assignments()
    cfg_mod._team_cfg_refreshing = False
    yield
    cfg_mod.reload_team_assignments()
    cfg_mod._team_cfg_refreshing = False


def test_an_expired_memo_on_the_event_loop_serves_stale_and_never_blocks(monkeypatch) -> None:
    builds: list[str] = []

    def slow_build():
        builds.append("build")
        time.sleep(0.4)  # stands in for the paginated Plaky call
        return cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="rebuilt")

    monkeypatch.setattr(cfg_mod, "_build_team_assignments", slow_build)

    async def scenario() -> None:
        # Prime the memo, then age it past the TTL.
        cached = cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="cached")
        cfg_mod._team_cfg_cache = (
            time.monotonic() - cfg_mod._TEAM_CFG_TTL_SECONDS - 5,
            cfg_mod._config_stamp(),
            cached,
        )
        started = time.perf_counter()
        got = cfg_mod.load_team_assignments()
        elapsed = time.perf_counter() - started

        assert got.plaky_field_engineer == "cached", "the previous answer is served"
        assert elapsed < 0.1, f"the event loop was blocked for {elapsed:.2f}s"
        # And the rebuild really did happen, off the loop.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if builds:
                break
        assert builds, "a background rebuild should have been scheduled"

    asyncio.run(scenario())


def test_off_the_event_loop_it_still_rebuilds_synchronously(monkeypatch) -> None:
    """A worker thread or a CLI has no loop to protect, so correctness wins."""
    monkeypatch.setattr(
        cfg_mod,
        "_build_team_assignments",
        lambda: cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="rebuilt"),
    )
    cfg_mod._team_cfg_cache = (
        time.monotonic() - cfg_mod._TEAM_CFG_TTL_SECONDS - 5,
        cfg_mod._config_stamp(),
        cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="cached"),
    )
    assert cfg_mod.load_team_assignments().plaky_field_engineer == "rebuilt"


def test_a_cold_memo_still_builds_even_on_the_loop(monkeypatch) -> None:
    """There is no previous answer to be right with, so blocking is correct here."""
    monkeypatch.setattr(
        cfg_mod,
        "_build_team_assignments",
        lambda: cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="fresh"),
    )

    async def scenario() -> None:
        cfg_mod._team_cfg_cache = None
        assert cfg_mod.load_team_assignments().plaky_field_engineer == "fresh"

    asyncio.run(scenario())


def test_a_burst_of_expired_reads_starts_one_rebuild(monkeypatch) -> None:
    builds: list[str] = []

    def build():
        builds.append("b")
        time.sleep(0.3)
        return cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="rebuilt")

    monkeypatch.setattr(cfg_mod, "_build_team_assignments", build)

    async def scenario() -> None:
        cfg_mod._team_cfg_cache = (
            time.monotonic() - cfg_mod._TEAM_CFG_TTL_SECONDS - 5,
            cfg_mod._config_stamp(),
            cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="cached"),
        )
        for _ in range(12):
            cfg_mod.load_team_assignments()
        await asyncio.sleep(0.6)
        assert len(builds) == 1, f"{len(builds)} rebuilds for one expiry"

    asyncio.run(scenario())


def test_a_failed_background_rebuild_keeps_the_previous_roster(monkeypatch) -> None:
    """A refresh that throws must never leave the process with no roster at all."""

    def boom():
        raise RuntimeError("plaky is down")

    monkeypatch.setattr(cfg_mod, "_build_team_assignments", boom)

    async def scenario() -> None:
        cached = cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="cached")
        cfg_mod._team_cfg_cache = (
            time.monotonic() - cfg_mod._TEAM_CFG_TTL_SECONDS - 5,
            cfg_mod._config_stamp(),
            cached,
        )
        assert cfg_mod.load_team_assignments() is cached
        await asyncio.sleep(0.4)
        assert cfg_mod._team_cfg_cache is not None
        assert cfg_mod._team_cfg_cache[2] is cached
        assert cfg_mod._team_cfg_refreshing is False, "the guard must reset after a failure"

    asyncio.run(scenario())


def test_an_edited_yaml_still_takes_effect_immediately(monkeypatch) -> None:
    """The stale path must not swallow a genuine config edit."""
    monkeypatch.setattr(
        cfg_mod,
        "_build_team_assignments",
        lambda: cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="rebuilt"),
    )

    async def scenario() -> None:
        cfg_mod._team_cfg_cache = (
            time.monotonic(),
            ("some", "other", "stamp"),  # a different file identity
            cfg_mod.TeamAssignmentsConfig(plaky_field_engineer="cached"),
        )
        assert cfg_mod.load_team_assignments().plaky_field_engineer == "rebuilt"

    asyncio.run(scenario())
