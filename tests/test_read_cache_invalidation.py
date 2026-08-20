"""Per-repo cache invalidation.

A push to one repo has to drop that repo's cached reads and nothing else. The trap is
casing: half the writers lowercase the slug and half pass it through, so a purge that
compares raw strings clears some of a repo's entries and leaves others, producing a
mixed-age context that is worse than a uniformly stale one.
"""

from __future__ import annotations

import asyncio

import pytest

from boardman.github import read_cache
from boardman.github.read_cache import cache_stats, clear_read_cache, invalidate_repo

# Every namespace that exists today, in both the lowercased and the as-passed form.
KEYS_FOR_BOARDMAN = [
    "structure:team-deepiri/deepiri-boardman",
    "planning:team-deepiri/deepiri-boardman:20",
    "defects:team-deepiri/deepiri-boardman",
    "repo-identity:Team-Deepiri/deepiri-boardman",
    "repo-tree:Team-Deepiri/deepiri-boardman@main",
    "hotspots:Team-Deepiri/deepiri-boardman@main:15",
    "file:Team-Deepiri/deepiri-boardman@main:README.md",
    "file:Team-Deepiri/deepiri-boardman@:DIRECTION.md",
]
KEYS_FOR_OTHERS = [
    "structure:team-deepiri/deepiri-axiom",
    "repo-tree:Team-Deepiri/deepiri-sorge@main",
    "file:Team-Deepiri/deepiri-axiom@main:README.md",
    "hotspots:Other-Org/deepiri-boardman@main:15",  # same repo name, different owner
]


@pytest.fixture(autouse=True)
def _clean():
    clear_read_cache()
    yield
    clear_read_cache()


def _seed(keys: list[str]) -> None:
    for k in keys:
        read_cache._entries[k] = (1.0e9, f"value for {k}")


def test_every_namespace_is_purged_for_the_repo() -> None:
    _seed(KEYS_FOR_BOARDMAN)
    assert cache_stats()["entries"] == len(KEYS_FOR_BOARDMAN)
    dropped = invalidate_repo("Team-Deepiri/deepiri-boardman")
    assert dropped == len(KEYS_FOR_BOARDMAN)
    assert cache_stats()["entries"] == 0


def test_casing_does_not_decide_which_half_survives() -> None:
    """The bug this test exists for: purging with the wrong casing half-clears."""
    _seed(KEYS_FOR_BOARDMAN)
    invalidate_repo("team-deepiri/DEEPIRI-BOARDMAN")
    assert cache_stats()["entries"] == 0


def test_other_repos_are_untouched() -> None:
    _seed(KEYS_FOR_BOARDMAN + KEYS_FOR_OTHERS)
    invalidate_repo("Team-Deepiri/deepiri-boardman")
    left = set(read_cache._entries)
    assert left == set(KEYS_FOR_OTHERS), "collateral damage or a survivor"


def test_a_different_owner_with_the_same_repo_name_survives() -> None:
    _seed(["hotspots:Other-Org/deepiri-boardman@main:15"])
    assert invalidate_repo("Team-Deepiri/deepiri-boardman") == 0
    assert cache_stats()["entries"] == 1


def test_a_repo_with_nothing_cached_is_a_no_op() -> None:
    assert invalidate_repo("Team-Deepiri/never-fetched") == 0


@pytest.mark.parametrize("bad", ["", "   ", "notaslug", "a/b/c/d"])
def test_a_malformed_name_never_clears_the_whole_cache(bad: str) -> None:
    """An empty or malformed repo name must not be read as "match everything"."""
    _seed(KEYS_FOR_BOARDMAN)
    invalidate_repo(bad)
    assert cache_stats()["entries"] == len(KEYS_FOR_BOARDMAN)


def test_an_in_flight_fetch_keeps_its_lock() -> None:
    """Dropping a held lock would orphan the caller waiting on it."""

    async def scenario() -> None:
        key = "structure:team-deepiri/deepiri-boardman"
        lock = read_cache._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        try:
            read_cache._entries[key] = (1.0e9, "v")
            invalidate_repo("Team-Deepiri/deepiri-boardman")
            assert key not in read_cache._entries, "the value should still be dropped"
            assert read_cache._locks.get(key) is lock, "the held lock must survive"
        finally:
            lock.release()

    asyncio.run(scenario())


def test_idle_locks_are_cleaned_up() -> None:
    key = "structure:team-deepiri/deepiri-boardman"
    read_cache._entries[key] = (1.0e9, "v")
    read_cache._locks[key] = asyncio.Lock()
    invalidate_repo("Team-Deepiri/deepiri-boardman")
    assert key not in read_cache._locks


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("structure:team-deepiri/x", "team-deepiri/x"),
        ("planning:Team-Deepiri/X:20", "team-deepiri/x"),
        ("repo-tree:Team-Deepiri/X@main", "team-deepiri/x"),
        ("file:Team-Deepiri/X@main:a/b/c.md", "team-deepiri/x"),
        ("hotspots:Team-Deepiri/X@main:15", "team-deepiri/x"),
        ("repo-identity:Team-Deepiri/X", "team-deepiri/x"),
        ("nocolonhere", ""),
        ("weird:notaslug", ""),
    ],
)
def test_key_repo_parsing(key: str, expected: str) -> None:
    assert read_cache._key_repo(key) == expected


# --- a purge that lands while a fetch is in flight ---------------------------------------


def test_a_fetch_started_before_a_push_does_not_write_its_answer_back() -> None:
    """The race the naive purge could not see: at t=0 a tree fetch starts and the key is
    not in _entries yet. At t=0.4 a push invalidates the repo and finds nothing to drop.
    At t=1.2 the fetch returns the PRE-push tree and caches it for five minutes."""

    async def scenario() -> None:
        key = "repo-tree:Team-Deepiri/deepiri-boardman@main"
        started = asyncio.Event()

        async def slow_fetch() -> str:
            started.set()
            await asyncio.sleep(0.05)
            return "tree as it was BEFORE the push"

        task = asyncio.create_task(read_cache.cached(key, slow_fetch, ok=lambda _v: True))
        await started.wait()
        assert key not in read_cache._entries, "precondition: nothing cached yet"

        invalidate_repo("Team-Deepiri/deepiri-boardman")
        value = await task

        assert value == "tree as it was BEFORE the push", "the caller still gets its answer"
        assert key not in read_cache._entries, "but the pre-push value must not be cached"

    asyncio.run(scenario())


def test_a_fetch_with_no_purge_still_caches_normally() -> None:
    async def scenario() -> None:
        key = "repo-tree:Team-Deepiri/deepiri-boardman@main"

        async def fetch() -> str:
            return "current tree"

        assert await read_cache.cached(key, fetch, ok=lambda _v: True) == "current tree"
        assert key in read_cache._entries

    asyncio.run(scenario())
