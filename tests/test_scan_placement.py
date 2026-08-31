"""Regression test: run_repo_scan must actually place created tasks on the board/group
that get_routing_async resolved, for every non-ambiguous routing source — not just
"explicit" (repos.yml). A prior bug discarded routing for "discovered:*" sources (a live
Plaky group whose name matched the repo slug), silently dropping board_id/group_id to
None and making every create_task call fail with "No Plaky board selected"."""

from __future__ import annotations

import pytest

from boardman.repos_config import RepoRouting
from boardman.services import scan_handler


class _FakeSession:
    def add(self, _obj):
        return None

    async def flush(self):
        return None

    async def execute(self, _q):
        class _Result:
            def scalar_one_or_none(self):
                return None

        return _Result()


async def _fake_fetch_scan_context(*_args, **_kwargs):
    return "direction", "commits", "issues", "plaky lines"


def _fake_scan_prompt(*_args, **_kwargs):
    return "prompt"


async def _fake_chat_complete(*_args, **_kwargs):
    return '[{"title": "Do the thing", "description": "desc", "priority": "medium"}]'


@pytest.mark.parametrize(
    "routing_source",
    ["explicit", "discovered:group_slug_match", "discovered:board_default_group"],
)
@pytest.mark.asyncio
async def test_non_ambiguous_routing_sources_place_the_created_task(monkeypatch, routing_source):
    routing = RepoRouting(
        category="devtools",
        plaky_table="Infrastructure",
        plaky_board_id="269028",
        plaky_group_id="933385",
    )

    async def fake_get_routing_async(*_args, **_kwargs):
        return routing, routing_source

    captured: dict[str, object] = {}

    class _FakePlaky:
        async def create_task(self, **kwargs):
            captured["board_id"] = kwargs.get("board_id")
            captured["group_id"] = kwargs.get("group_id")
            return {"ok": True}

    async def fake_resolve_group_for_repo(_bid, _short, *, fallback_group_id, plaky):
        return fallback_group_id

    async def fake_board_person_field_keys(_bid):
        return {"qa": "person-1"}

    async def fake_build_assignment_field_map(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(scan_handler, "get_routing_async", fake_get_routing_async)
    monkeypatch.setattr(scan_handler, "_fetch_scan_context", _fake_fetch_scan_context)
    monkeypatch.setattr(scan_handler, "_scan_prompt", _fake_scan_prompt)
    monkeypatch.setattr(scan_handler, "chat_complete", _fake_chat_complete)
    monkeypatch.setattr(scan_handler, "PlakyClient", lambda: _FakePlaky())
    monkeypatch.setattr(scan_handler, "build_assignment_field_map", fake_build_assignment_field_map)
    import boardman.plaky.board_aware as board_aware

    monkeypatch.setattr(board_aware, "resolve_group_for_repo", fake_resolve_group_for_repo)
    monkeypatch.setattr(board_aware, "board_person_field_keys", fake_board_person_field_keys)
    monkeypatch.setattr(scan_handler.settings, "github_pat", "fake-pat")
    monkeypatch.setattr(scan_handler.settings, "llm_provider", "openrouter")
    monkeypatch.setattr(scan_handler.settings, "llm_model", "some/model")

    result = await scan_handler.run_repo_scan(_FakeSession(), "Team-Deepiri/deepiri-axiom", dry_run=False)

    assert result["ok"] is True
    assert result["tasks_created"] == 1
    assert captured["board_id"] == "269028"
    assert captured["group_id"] == "933385"


@pytest.mark.parametrize("routing_source", ["org_default", "none", "discovered:none"])
@pytest.mark.asyncio
async def test_ambiguous_routing_sources_do_not_auto_place(monkeypatch, routing_source):
    routing = RepoRouting(
        category="devtools",
        plaky_table="Infrastructure",
        plaky_board_id="269028",
        plaky_group_id="933385",
    )

    async def fake_get_routing_async(*_args, **_kwargs):
        return routing, routing_source

    captured: dict[str, object] = {}

    class _FakePlaky:
        async def create_task(self, **kwargs):
            captured["board_id"] = kwargs.get("board_id")
            captured["group_id"] = kwargs.get("group_id")
            return {"ok": False, "message": "No Plaky board selected"}

    async def fake_build_assignment_field_map(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(scan_handler, "get_routing_async", fake_get_routing_async)
    monkeypatch.setattr(scan_handler, "_fetch_scan_context", _fake_fetch_scan_context)
    monkeypatch.setattr(scan_handler, "_scan_prompt", _fake_scan_prompt)
    monkeypatch.setattr(scan_handler, "chat_complete", _fake_chat_complete)
    monkeypatch.setattr(scan_handler, "PlakyClient", lambda: _FakePlaky())
    monkeypatch.setattr(scan_handler, "build_assignment_field_map", fake_build_assignment_field_map)
    monkeypatch.setattr(scan_handler.settings, "github_pat", "fake-pat")
    monkeypatch.setattr(scan_handler.settings, "llm_provider", "openrouter")
    monkeypatch.setattr(scan_handler.settings, "llm_model", "some/model")

    result = await scan_handler.run_repo_scan(_FakeSession(), "Team-Deepiri/deepiri-axiom", dry_run=False)

    assert result["ok"] is True
    assert captured["board_id"] is None
    assert captured["group_id"] is None
