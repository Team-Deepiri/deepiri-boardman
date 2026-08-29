"""Live-measured QA hardware capability, read from Plaky instead of hand-typed
team_assignments.yml `tier` — see boardman/assignment/capability_board.py."""

from __future__ import annotations

import pytest

from boardman.assignment import capability_board as cb
from boardman.diagnostics.hardware_probe import HardwareSnapshot, capability_tier


def test_capability_tier_thresholds() -> None:
    assert capability_tier(HardwareSnapshot(cores=16, ram_gb=32, has_gpu=True)) == "heavy"
    assert capability_tier(HardwareSnapshot(cores=8, ram_gb=16, has_gpu=False)) == "standard"
    assert capability_tier(HardwareSnapshot(cores=2, ram_gb=4, has_gpu=False)) == "light"
    # GPU alone isn't enough without RAM/cores to back it.
    assert capability_tier(HardwareSnapshot(cores=2, ram_gb=4, has_gpu=True)) == "light"


_SCHEMA = {
    "ok": True,
    "normalized": {
        "fields": [
            {"key": "f-login", "name": "GitHub Login", "type": "text"},
            {
                "key": "f-tier",
                "name": "Tier",
                "type": "choice",
                "options": [
                    {"id": "opt-light", "name": "light"},
                    {"id": "opt-standard", "name": "standard"},
                    {"id": "opt-heavy", "name": "heavy"},
                ],
            },
        ]
    },
}


@pytest.mark.asyncio
async def test_fetch_capability_tiers_reads_choice_field(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "board-1")

    async def fake_schema(board_id):
        assert board_id == "board-1"
        return _SCHEMA

    monkeypatch.setattr(cb, "fetch_board_schema_bundle", fake_schema)

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {
                "ok": True,
                "items": [
                    {"id": "1", "fields": {"f-login": "octocat", "f-tier": "opt-heavy"}},
                    {"id": "2", "fields": {"f-login": "Other-User", "f-tier": "opt-light"}},
                    {"id": "3", "fields": {"f-login": "", "f-tier": "opt-light"}},
                ],
            }

    tiers = await cb.fetch_capability_tiers(FakePlaky())
    assert tiers == {"octocat": "heavy", "other-user": "light"}


@pytest.mark.asyncio
async def test_fetch_capability_tiers_empty_when_board_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "")
    assert await cb.fetch_capability_tiers() == {}


@pytest.mark.asyncio
async def test_fetch_capability_tiers_empty_when_schema_unresolvable(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "board-1")

    async def fake_schema(board_id):
        return {"ok": False}

    monkeypatch.setattr(cb, "fetch_board_schema_bundle", fake_schema)
    assert await cb.fetch_capability_tiers() == {}


@pytest.mark.asyncio
async def test_resolve_hardware_tier_falls_back_when_no_live_row(monkeypatch) -> None:
    monkeypatch.setattr(cb, "fetch_capability_tiers", lambda: _async_return({"someone": "heavy"}))
    assert await cb.resolve_hardware_tier("unknown-login", "standard") == "standard"
    assert await cb.resolve_hardware_tier("someone", "standard") == "heavy"


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_report_hardware_capability_creates_new_row(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "board-1")
    monkeypatch.setattr(cb.settings, "plaky_capability_group_id", "")

    async def fake_schema(board_id):
        return _SCHEMA

    monkeypatch.setattr(cb, "fetch_board_schema_bundle", fake_schema)

    created = {}

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {"ok": True, "items": []}

        async def create_task(self, **kw):
            created.update(kw)
            return {"ok": True, "task": {"id": "new-1"}}

    result = await cb.report_hardware_capability(
        github_login="octocat", tier="heavy", cores=16, ram_gb=32.0, has_gpu=True, plaky=FakePlaky()
    )
    assert result["ok"] is True
    assert result["action"] == "created"
    assert created["field_values"]["f-login"] == "octocat"
    assert created["field_values"]["f-tier"] == "opt-heavy"


@pytest.mark.asyncio
async def test_report_hardware_capability_updates_existing_row(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "board-1")

    async def fake_schema(board_id):
        return _SCHEMA

    monkeypatch.setattr(cb, "fetch_board_schema_bundle", fake_schema)

    patched = {}

    class FakePlaky:
        async def list_board_items(self, *a, **k):
            return {"ok": True, "items": [{"id": "42", "fields": {"f-login": "octocat"}}]}

        async def patch_item_field_values(self, board_id, item_id, field_values, **kw):
            patched.update(board_id=board_id, item_id=item_id, field_values=field_values)
            return {"ok": True}

    result = await cb.report_hardware_capability(
        github_login="octocat", tier="light", plaky=FakePlaky()
    )
    assert result["ok"] is True
    assert result["action"] == "updated"
    assert patched["item_id"] == "42"
    assert patched["field_values"]["f-tier"] == "opt-light"


@pytest.mark.asyncio
async def test_report_hardware_capability_rejects_bad_tier(monkeypatch) -> None:
    monkeypatch.setattr(cb.settings, "plaky_capability_board_id", "board-1")
    result = await cb.report_hardware_capability(github_login="octocat", tier="super-fast")
    assert result["ok"] is False
