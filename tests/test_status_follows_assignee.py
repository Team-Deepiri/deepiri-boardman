"""Status must agree with the Assignee at write time.

The demo showed a task created WITH a developer that still said NEEDS ASSIGNED — the two
columns contradicted each other and nobody's workflow moved. The audit found the
PR-webhook path was the ONLY one of ~20 assignee-writing paths that synced status; these
tests pin the rule for the create/update/patch family, plus the QA-column collision the
same audit surfaced.
"""

from __future__ import annotations

from typing import Any

import pytest

from boardman.services import task_mutations as tm

BOARD = "269028"


def _rp(intent_map: dict[str, tuple[str, str]]):
    async def fake(bid: str, *, intent: str):
        return intent_map.get(intent)

    return fake


@pytest.fixture()
def _intents(monkeypatch):
    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch",
        _rp(
            {
                "workflow_assigned": ("status-8", "8"),
                "workflow_needs_assigned": ("status-8", "0"),
            }
        ),
    )


@pytest.mark.asyncio
async def test_engineer_written_defaults_to_assigned(_intents) -> None:
    fv: dict[str, Any] = {"status-8": "2"}  # the canonical DEFAULT ladder's In Progress
    out = await tm._status_follow_assignee(BOARD, fv, engineer_written=True, explicit_status=False)
    assert fv["status-8"] == "8"
    assert out and "status_follows_assignee" in out


@pytest.mark.asyncio
async def test_no_engineer_defaults_to_needs_assigned(_intents) -> None:
    fv: dict[str, Any] = {"status-8": "2"}
    await tm._status_follow_assignee(BOARD, fv, engineer_written=False, explicit_status=False)
    assert fv["status-8"] == "0"


@pytest.mark.asyncio
async def test_explicit_needs_assigned_plus_engineer_is_a_contradiction(_intents) -> None:
    """Asking for both an assignee and NEEDS ASSIGNED cannot both be honored; the person
    is the stronger signal, exactly the demo's broken state."""
    fv: dict[str, Any] = {"status-8": "0"}
    out = await tm._status_follow_assignee(BOARD, fv, engineer_written=True, explicit_status=True)
    assert fv["status-8"] == "8"
    assert out and out["status_follows_assignee"].get("overrode")


@pytest.mark.asyncio
async def test_explicit_other_status_is_never_touched(_intents) -> None:
    fv: dict[str, Any] = {"status-8": "2"}  # explicit In Progress
    out = await tm._status_follow_assignee(BOARD, fv, engineer_written=True, explicit_status=True)
    assert fv["status-8"] == "2"
    assert out is None


@pytest.mark.asyncio
async def test_bump_moves_only_from_needs_assigned(_intents, monkeypatch) -> None:
    patches: list[dict] = []

    class FakePlaky:
        def __init__(self, status: str):
            self._status = status

        async def get_board_item_public(self, bid, tid):
            return {
                "ok": True,
                "item": {"fields": [{"key": "status-8", "type": "STATUS", "value": self._status}]},
            }

        async def patch_item_field_values(self, bid, tid, values):
            patches.append(values)
            return {"ok": True}

    out = await tm.bump_status_for_assignee(BOARD, "t1", FakePlaky("0"))
    assert out and patches == [{"status-8": "8"}]

    patches.clear()
    out2 = await tm.bump_status_for_assignee(BOARD, "t1", FakePlaky("4"))  # Needs QA
    assert out2 is None and patches == [], "bump downgraded a task already in review"


@pytest.mark.asyncio
async def test_qa_pick_never_lands_in_the_assignee_column() -> None:
    """Board with person columns but none named qa/quality: the old fallback wrote the QA
    pick into the FIRST person column — the Assignee — handing ownership to the reviewer."""
    normalized = {
        "fields": [
            {"key": "person-1", "name": "Assignee", "type": "PERSON"},
            {"key": "person-2", "name": "Collaborators", "type": "PERSON"},
        ]
    }
    eng, qa = await tm._infer_plaky_person_column_keys("b1", "", "", normalized=normalized)
    assert eng == "person-1"
    assert qa == "", "QA fell back into a person column that is not a QA column"
