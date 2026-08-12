"""Caching must never make the assistant faster at being wrong.

Two speed fixes land here: a process-wide board->space map and a short-TTL cache for
read-only GitHub context. Both are only safe if a failure is never what gets remembered,
and if the thing being cached genuinely does not change between two questions.

Also covers the three ways "create me some tasks" used to fail.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from boardman.github import read_cache as rc


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_read_cache_ttl_seconds", 300.0)
    rc.clear_read_cache()
    yield
    rc.clear_read_cache()


@pytest.mark.asyncio
async def test_second_question_about_a_repo_does_not_refetch() -> None:
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        return json.dumps({"ok": True, "repo": "o/r"})

    a = await rc.cached("planning:o/r", fetch, ok=rc.json_ok)
    b = await rc.cached("planning:o/r", fetch, ok=rc.json_ok)
    assert a == b
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_failed_fetch_is_never_cached() -> None:
    """A 403 pinned for five minutes turns one transient blip into a conversation where
    the assistant insists it cannot read a repo it can read."""
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"ok": False, "message": "403 rate limited"})
        return json.dumps({"ok": True, "repo": "o/r"})

    first = await rc.cached("planning:o/r", flaky, ok=rc.json_ok)
    assert json.loads(first)["ok"] is False
    second = await rc.cached("planning:o/r", flaky, ok=rc.json_ok)
    assert json.loads(second)["ok"] is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_fetch() -> None:
    calls = {"n": 0}

    async def slow() -> str:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return json.dumps({"ok": True})

    await asyncio.gather(*[rc.cached("k", slow, ok=rc.json_ok) for _ in range(5)])
    assert calls["n"] == 1, "concurrent callers stampeded the API"


@pytest.mark.asyncio
async def test_ttl_zero_disables_the_cache(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_read_cache_ttl_seconds", 0.0)
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        return json.dumps({"ok": True})

    await rc.cached("k", fetch, ok=rc.json_ok)
    await rc.cached("k", fetch, ok=rc.json_ok)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_expired_entry_is_refetched(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_read_cache_ttl_seconds", 0.05)
    calls = {"n": 0}

    async def fetch() -> str:
        calls["n"] += 1
        return json.dumps({"ok": True})

    await rc.cached("k", fetch, ok=rc.json_ok)
    await asyncio.sleep(0.08)
    await rc.cached("k", fetch, ok=rc.json_ok)
    assert calls["n"] == 2


# --- board -> space cache -----------------------------------------------------------


@pytest.mark.asyncio
async def test_space_lookup_is_shared_across_client_instances(monkeypatch) -> None:
    """Callers build PlakyClient() fresh everywhere. With a per-instance map each call
    re-listed every space and every board first, and one hiccup in that walk surfaced as
    'Could not resolve space for board' - i.e. the assistant cannot create tasks."""
    from boardman.plaky import client as pc

    pc.clear_space_cache()
    calls = {"n": 0}

    async def fake_list_boards(self):
        calls["n"] += 1
        self._board_to_space["269031"] = "185467"
        return {"ok": True, "boards": [{"id": "269031", "space_id": "185467"}]}

    monkeypatch.setattr(pc.PlakyClient, "list_boards", fake_list_boards)

    first = await pc.PlakyClient().resolve_space_for_board("269031")
    second = await pc.PlakyClient().resolve_space_for_board("269031")
    assert first == second == "185467"
    assert calls["n"] == 1, "a fresh client re-walked every space"

    pc.clear_space_cache()
    await pc.PlakyClient().resolve_space_for_board("269031")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_concurrent_space_lookups_refresh_once(monkeypatch) -> None:
    from boardman.plaky import client as pc

    pc.clear_space_cache()
    calls = {"n": 0}

    async def fake_list_boards(self):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        self._board_to_space["269031"] = "185467"
        return {"ok": True, "boards": []}

    monkeypatch.setattr(pc.PlakyClient, "list_boards", fake_list_boards)
    await asyncio.gather(*[pc.PlakyClient().resolve_space_for_board("269031") for _ in range(4)])
    assert calls["n"] == 1


# --- create task: the three ways it used to fail ------------------------------------


@pytest.mark.asyncio
async def test_create_without_a_board_explains_instead_of_404ing(monkeypatch) -> None:
    """It used to POST to a bare /tasks path the public API does not serve, so the user
    got 'No static resource v1/public/tasks' and no hint that a board was missing."""
    from boardman.plaky import client as pc

    monkeypatch.setattr(pc, "context_board_id", lambda: "")
    monkeypatch.setattr(pc, "context_group_id", lambda: "")
    c = pc.PlakyClient(api_key="k")

    async def boom(*a, **kw):
        raise AssertionError("must not hit the network without a board")

    monkeypatch.setattr(pc, "_request_with_rate_limit_retry", boom)

    res = await c.create_task("t", "d")
    assert res["ok"] is False
    assert res.get("needs_board") is True
    assert "board" in res["message"].lower()


@pytest.mark.asyncio
async def test_create_with_board_but_no_group_picks_the_first_group(monkeypatch) -> None:
    from boardman.plaky import client as pc

    monkeypatch.setattr(pc, "context_group_id", lambda: "")
    c = pc.PlakyClient(api_key="k")
    used: dict[str, str] = {}

    async def fake_groups(self, bid):
        return {"ok": True, "groups": [{"id": "907471", "name": "Open PRs"}]}

    async def fake_create(self, bid, gid, title, desc, prio):
        used["board"], used["group"] = bid, gid
        return {"ok": True, "task_id": "1", "task": {"id": "1"}}

    monkeypatch.setattr(pc.PlakyClient, "list_groups", fake_groups)
    monkeypatch.setattr(pc.PlakyClient, "_create_item_hierarchy", fake_create)

    res = await c.create_task("t", "", board_id="269031")
    assert res["ok"] is True
    assert used == {"board": "269031", "group": "907471"}


@pytest.mark.asyncio
async def test_create_on_a_board_with_no_groups_says_so(monkeypatch) -> None:
    from boardman.plaky import client as pc

    monkeypatch.setattr(pc, "context_group_id", lambda: "")
    c = pc.PlakyClient(api_key="k")

    async def no_groups(self, bid):
        return {"ok": False, "groups": [], "message": "Could not list groups"}

    monkeypatch.setattr(pc.PlakyClient, "list_groups", no_groups)

    res = await c.create_task("t", "d", board_id="269031")
    assert res["ok"] is False
    assert res.get("needs_group") is True
    assert "group" in res["message"].lower()


@pytest.mark.asyncio
async def test_pr_review_state_is_never_cached() -> None:
    """Merge-readiness changes while you are looking at it. Only static repo context is
    cached; the PR reader must always read live."""
    import inspect

    from boardman.agent.tools import github_tools as gt

    src = inspect.getsource(gt._github_read_pull_request)
    assert "cached(" not in src
