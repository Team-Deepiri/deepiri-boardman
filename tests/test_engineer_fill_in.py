"""Engineer (developer) assignee fill-in via update_task_internal + PR-open helper."""

from __future__ import annotations

from typing import Any, Dict

import pytest

import boardman.services.task_mutations as tm
from boardman.services.task_mutations import UpdateTaskInput, update_task_internal


_SCHEMA = {
    "normalized": {
        "board_name": "diri-cyrex",
        "fields": [
            {"key": "person-3", "name": "Assignee", "type": "PERSON"},
            {"key": "person-4", "name": "QA Engineer Assigned", "type": "PERSON"},
            {
                "key": "status-6",
                "name": "Status",
                "type": "STATUS",
                "options": [
                    {"name": "Assigned", "id": "opt-assigned"},
                    {"name": "Needs QA", "id": "opt-needs-qa"},
                ],
            },
        ],
    },
    "ok": True,
}


class _FakePlaky:
    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    async def patch_item_field_values(self, board_id, item_id, values, **kwargs):
        self.patches.append((item_id, dict(values)))
        return {"ok": True}

    async def get_task(self, task_id):
        return {"ok": True, "task": {"boardId": "269558", "id": task_id}}

    async def update_task_fields(self, task_id, **kwargs):
        return {"ok": True}


@pytest.fixture
def fake_plaky(monkeypatch: pytest.MonkeyPatch) -> _FakePlaky:
    fake = _FakePlaky()
    monkeypatch.setattr(tm, "PlakyClient", lambda: fake)

    async def _bundle(_bid):
        return _SCHEMA

    async def _noop_sync(_bid):
        return {"ok": True}

    monkeypatch.setattr(tm, "fetch_board_schema_bundle", _bundle)
    monkeypatch.setattr(tm, "sync_team_assignment_field_keys_from_board", _noop_sync)
    return fake


@pytest.mark.asyncio
async def test_engineer_fill_in_patches_assignee_field(fake_plaky: _FakePlaky):
    res = await update_task_internal(
        "task-1",
        UpdateTaskInput(
            engineer_plaky_id="plaky-dev-9",
            engineer_plaky_field_key="person-3",
            plaky_board_id="269558",
        ),
    )
    assert res["ok"] is True
    assert fake_plaky.patches == [("task-1", {"person-3": "plaky-dev-9"})]


@pytest.mark.asyncio
async def test_engineer_field_key_inferred_when_not_supplied(fake_plaky: _FakePlaky):
    # No explicit key, no team_assignments engineer key -> inferred from schema (Assignee=person-3).
    res = await update_task_internal(
        "task-2",
        UpdateTaskInput(engineer_plaky_id="plaky-dev-9", plaky_board_id="269558"),
    )
    assert res["ok"] is True
    assert fake_plaky.patches and fake_plaky.patches[0][1].get("person-3") == "plaky-dev-9"


@pytest.mark.asyncio
async def test_engineer_and_assigned_status_together(fake_plaky: _FakePlaky):
    # Mirrors the PR-open helper: it resolves (status_field_key, option_id) via
    # resolve_plaky_status_patch and passes both — bypassing label canonicalization.
    res = await update_task_internal(
        "task-3",
        UpdateTaskInput(
            engineer_plaky_id="plaky-dev-9",
            engineer_plaky_field_key="person-3",
            status="opt-assigned",
            status_plaky_field_key="status-6",
            plaky_board_id="269558",
        ),
    )
    assert res["ok"] is True
    _, values = fake_plaky.patches[0]
    assert values.get("person-3") == "plaky-dev-9"
    assert values.get("status-6") == "opt-assigned"
