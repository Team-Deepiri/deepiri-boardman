"""GitHub sidebar priority + issue edits + reopen resume (live failures, 2026-08-19).

Ali set Priority=High in GitHub's sidebar issue field on issue #87; the payload key
`issue_field_values` was silently dropped by the pydantic model, no label existed, so
the task sat on the text-inferred Medium. Title/body edits never re-synced (the poller
signature ignored text). Reopening an unassigned issue wrote In Progress with an empty
Assignee column instead of resuming the pre-close state.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from boardman.database.models import Base, IssueTaskMap, SyncLog
from boardman.github.webhooks import IssueEventPayload
from boardman.services import issue_handler as ih
from boardman.services.github_poller import issue_meta_signature, issue_text_signature
from boardman.services.sync_state import resolve_issue_state

# The exact shape GitHub returns for issue #87 (verified live).
_FIELD_HIGH = [
    {
        "issue_field_id": 37469770,
        "data_type": "single_select",
        "issue_field_name": "Priority",
        "value": 65565980,
        "single_select_option": {"id": 65565980, "name": "High", "color": "red"},
    }
]


def _issue(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 87,
        "title": "Speed Issue",
        "body": "things are slow",
        "html_url": "https://github.com/Team-Deepiri/deepiri-boardman/issues/87",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    base.update(over)
    return base


# --- priority resolution ---------------------------------------------------------


def test_sidebar_priority_field_wins() -> None:
    st = resolve_issue_state(
        _issue(issue_field_values=_FIELD_HIGH), repo_full_name="o/r", repo_name="r"
    )
    assert st.priority == "High"
    assert st.priority_explicit is True


def test_urgent_maps_to_very_important() -> None:
    fields = [dict(_FIELD_HIGH[0], single_select_option={"id": 1, "name": "Urgent"})]
    st = resolve_issue_state(_issue(issue_field_values=fields), repo_full_name="o/r", repo_name="r")
    assert st.priority == "Very Important"
    assert st.priority_explicit is True


def test_low_and_medium_map_directly() -> None:
    for name, want in (("Low", "Low"), ("Medium", "Medium")):
        fields = [dict(_FIELD_HIGH[0], single_select_option={"id": 1, "name": name})]
        st = resolve_issue_state(
            _issue(issue_field_values=fields), repo_full_name="o/r", repo_name="r"
        )
        assert (st.priority, st.priority_explicit) == (want, True)


def test_priority_label_is_explicit_too() -> None:
    st = resolve_issue_state(
        _issue(labels=[{"name": "priority: low"}]), repo_full_name="o/r", repo_name="r"
    )
    assert (st.priority, st.priority_explicit) == ("Low", True)


def test_no_signal_falls_back_to_inference_and_is_not_explicit() -> None:
    st = resolve_issue_state(_issue(), repo_full_name="o/r", repo_name="r")
    assert st.priority == "Medium"
    assert st.priority_explicit is False


def test_webhook_model_keeps_issue_field_values() -> None:
    """pydantic drops undeclared keys — this is the bug that ate the signal."""
    p = IssueEventPayload(
        action="edited",
        issue=_issue(issue_field_values=_FIELD_HIGH),
        repository={"full_name": "o/r", "name": "r"},
    )
    st = resolve_issue_state(p.issue, repo_full_name="o/r", repo_name="r")
    assert st.priority == "High" and st.priority_explicit


# --- poller change signatures ----------------------------------------------------


def test_meta_signature_sees_priority_changes() -> None:
    a = issue_meta_signature(_issue(issue_field_values=_FIELD_HIGH))
    fields = [dict(_FIELD_HIGH[0], single_select_option={"id": 2, "name": "Urgent"})]
    b = issue_meta_signature(_issue(issue_field_values=fields))
    assert a != b


def test_text_signature_sees_title_and_body_edits() -> None:
    base = _issue()
    assert issue_text_signature(base) == issue_text_signature(_issue())
    assert issue_text_signature(base) != issue_text_signature(_issue(title="New title"))
    assert issue_text_signature(base) != issue_text_signature(_issue(body="rewritten"))


# --- handler behavior --------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class _FakePlaky:
    async def add_comment(self, task_id: str, body: str, **kw: Any) -> dict:
        return {"ok": True}


def _payload(action: str, issue: dict[str, Any]) -> IssueEventPayload:
    return IssueEventPayload(
        action=action, issue=issue, repository={"full_name": "o/r", "name": "r"}
    )


@pytest.fixture()
def captured_updates(monkeypatch) -> list[Any]:
    updates: list[Any] = []

    async def fake_update(task_id: str, inp: Any) -> dict:
        updates.append(inp)
        return {"ok": True}

    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)
    return updates


@pytest.fixture()
def plumbing(monkeypatch) -> None:
    class _Routing:
        plaky_board_id = "B1"
        plaky_group_id = "G1"
        plaky_table = ""
        category = ""

    async def fake_routing(*a: Any, **kw: Any) -> Any:
        return _Routing()

    monkeypatch.setattr(ih, "get_routing_async", fake_routing)
    monkeypatch.setattr(ih, "effective_plaky_placement", lambda r: ("B1", "G1"))
    monkeypatch.setattr(ih, "PlakyClient", _FakePlaky)


@pytest.mark.asyncio
async def test_explicit_priority_reaches_the_update(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    res = await ih.handle_issue_labels_changed(
        _payload("labeled", _issue(issue_field_values=_FIELD_HIGH)), db_session
    )
    assert res["ok"] is True
    assert captured_updates[0].priority == "High"


@pytest.mark.asyncio
async def test_inferred_priority_never_rides_metadata_events(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """No explicit GitHub signal → the board's hand-tuned priority must survive."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_labels_changed(_payload("labeled", _issue()), db_session)
    assert captured_updates[0].priority is None


@pytest.mark.asyncio
async def test_edited_syncs_title_and_body(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_edited(
        _payload("edited", _issue(title="Renamed", body="new body")), db_session
    )
    inp = captured_updates[0]
    assert inp.title == "[r] Renamed"
    assert "new body" in (inp.description or "")


@pytest.mark.asyncio
async def test_untitleable_board_mirrors_the_edit_as_a_comment(
    db_session, plumbing, monkeypatch
) -> None:
    """Plaky answers `Allow: GET,HEAD,DELETE,OPTIONS` on an item, so a rename cannot be
    written. Verified live on board 269028. The edit must still reach the board."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def fake_update(task_id: str, inp: Any) -> dict:
        return {
            "ok": False,
            "operations": {
                "field_diff": {"ok": True},
                "item_text_fields": {"ok": False, "message": "board has no title field"},
            },
        }

    mirrored: list[dict[str, Any]] = []

    async def fake_mirror(session: Any, plaky: Any, **kw: Any) -> dict:
        mirrored.append(kw)
        return {"ok": True}

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)
    monkeypatch.setattr(ih, "mirror_github_activity", fake_mirror)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)

    res = await ih.handle_issue_edited(
        _payload("edited", _issue(title="Speed Issue", body="now with detail")), db_session
    )
    assert res["text_mirrored_as_comment"] is True
    assert res["ok"] is True  # a Plaky API limit is not a sync failure
    assert "Speed Issue" in mirrored[0]["body"]
    assert "now with detail" in mirrored[0]["body"]


@pytest.mark.asyncio
async def test_metadata_only_events_never_comment(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """A label change carries no text, so it must not spam the task with comments."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def boom(*a: Any, **kw: Any):
        raise AssertionError("no comment for metadata-only events")

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr(ih, "mirror_github_activity", boom)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_labels_changed(_payload("labeled", _issue()), db_session)


@pytest.mark.asyncio
async def test_close_records_the_pre_close_status(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        assert intent == "workflow_completed"
        return ("status-8", "1")

    async def fake_current(plaky: Any, bid: str, tid: str, key: str) -> str:
        return "4"  # the task sat on Needs QA when the issue closed

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    monkeypatch.setattr("boardman.services.pr_handler._current_status_value", fake_current)

    res = await ih.handle_issue_closed(_payload("closed", _issue()), db_session)
    assert res["ok"] is True and res["status"] == "1"
    row = (
        (await db_session.execute(select(SyncLog).where(SyncLog.action == "issue_closed")))
        .scalars()
        .one()
    )
    detail = json.loads(row.detail)
    assert detail["previous_status_value"] == "4"
    assert detail["previous_status_key"] == "status-8"


@pytest.mark.asyncio
async def test_reopen_resumes_the_stored_pre_close_status(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_closed",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"previous_status_value": "4", "previous_status_key": "status-8"}),
        )
    )
    await db_session.flush()

    async def fail_resolve(bid: str, *, intent: str):
        raise AssertionError("stored state must restore without the intent ladder")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fail_resolve)
    res = await ih.handle_issue_reopened(
        _payload("reopened", _issue(assignees=[{"login": "Blasted-ctrl"}])), db_session
    )
    assert res["ok"] is True and res["status"] == "4"
    assert captured_updates[0].status == "4"
    assert captured_updates[0].status_plaky_field_key == "status-8"


@pytest.mark.asyncio
async def test_reopen_without_history_and_no_assignee_needs_assigned(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """The live complaint: reopened + unassigned must NEVER read In Progress."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    intents: list[str] = []

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        intents.append(intent)
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    res = await ih.handle_issue_reopened(_payload("reopened", _issue()), db_session)
    assert res["status"] == "0"
    assert intents == ["workflow_needs_assigned"]


@pytest.mark.asyncio
async def test_reopen_without_history_with_assignee_goes_assigned(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    await db_session.flush()

    intents: list[str] = []

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        intents.append(intent)
        return ("status-8", "8")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_reopened(
        _payload("reopened", _issue(assignees=[{"login": "Blasted-ctrl"}])), db_session
    )
    assert intents == ["workflow_assigned"]


@pytest.mark.asyncio
async def test_reopen_never_restores_a_working_status_without_an_owner(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """Review-confirmed hole: assignee removed while the issue sat closed. Restoring
    the stored In Progress would rebuild the exact broken state Ali reported (working
    status, empty Assignee column)."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_closed",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"previous_status_value": "2", "previous_status_key": "status-8"}),
        )
    )
    await db_session.flush()

    intents: list[str] = []

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        intents.append(intent)
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    res = await ih.handle_issue_reopened(_payload("reopened", _issue()), db_session)
    assert intents == ["workflow_needs_assigned"]
    assert res["status"] == "0"
    assert captured_updates[0].status == "0"


@pytest.mark.asyncio
async def test_duplicate_close_delivery_does_not_bury_the_capture(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """A redelivered `closed` writes a blank capture (the task already reads
    Completed). Reading only the newest row lost the real one."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    for detail in (
        {"previous_status_value": "5", "previous_status_key": "status-8"},
        {"previous_status_value": "", "previous_status_key": "status-8"},
    ):
        db_session.add(
            SyncLog(
                action="issue_closed",
                github_repo="r",
                github_ref="87",
                plaky_task_id="t-1",
                detail=json.dumps(detail),
            )
        )
    await db_session.flush()

    async def fail_resolve(bid: str, *, intent: str):
        raise AssertionError("the real capture must be found")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fail_resolve)
    res = await ih.handle_issue_reopened(
        _payload("reopened", _issue(assignees=[{"login": "Blasted-ctrl"}])), db_session
    )
    assert res["status"] == "5"


@pytest.mark.asyncio
async def test_capture_from_a_finished_cycle_is_not_reused(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """close(In QA) -> reopen -> task completed by hand -> close(blank) -> reopen:
    the stale two-cycles-old capture must not come back."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_closed",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"previous_status_value": "5", "previous_status_key": "status-8"}),
        )
    )
    await db_session.flush()
    db_session.add(
        SyncLog(action="issue_reopened", github_repo="r", github_ref="87", plaky_task_id="t-1")
    )
    await db_session.flush()
    db_session.add(
        SyncLog(
            action="issue_closed",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"previous_status_value": "", "previous_status_key": "status-8"}),
        )
    )
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "8")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    res = await ih.handle_issue_reopened(
        _payload("reopened", _issue(assignees=[{"login": "Blasted-ctrl"}])), db_session
    )
    assert res["status"] == "8"  # ladder, not the stale "5"


@pytest.mark.asyncio
async def test_replayed_open_does_not_stomp_a_hand_tuned_priority(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """The poller re-emits `opened` for every issue in its catch-up window on each
    restart. With a healthy post-create patch, that replay must not rewrite a lead's
    board priority back to the text-inferred Medium."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_created",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"post_create_update_ok": True}),
        )
    )
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_opened(_payload("opened", _issue()), db_session)
    assert captured_updates[0].priority is None


@pytest.mark.asyncio
async def test_replayed_open_repairs_a_failed_post_create_patch(
    db_session, captured_updates, plumbing, monkeypatch
) -> None:
    """That replay is exactly how a task whose creation patch failed gets repaired."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_created",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"post_create_update_ok": False}),
        )
    )
    await db_session.flush()

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        return ("status-8", "0")

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    await ih.handle_issue_opened(_payload("opened", _issue()), db_session)
    assert captured_updates[0].priority == "Medium"


@pytest.mark.asyncio
async def test_reopen_falls_back_when_the_stored_option_died(
    db_session, plumbing, monkeypatch
) -> None:
    """Board vocab changed since the close → restore fails → intent ladder rescues."""
    db_session.add(IssueTaskMap(github_repo="r", github_issue_number=87, plaky_task_id="t-1"))
    db_session.add(
        SyncLog(
            action="issue_closed",
            github_repo="r",
            github_ref="87",
            plaky_task_id="t-1",
            detail=json.dumps({"previous_status_value": "99", "previous_status_key": "status-8"}),
        )
    )
    await db_session.flush()

    attempts: list[str] = []

    async def fake_update(task_id: str, inp: Any) -> dict:
        attempts.append(inp.status)
        return {"ok": inp.status != "99"}

    async def fake_resolve(bid: str, *, intent: str) -> tuple[str, str]:
        assert intent == "workflow_assigned"
        return ("status-8", "8")

    monkeypatch.setattr("boardman.services.task_mutations.update_task_internal", fake_update)
    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.resolve_plaky_status_patch", fake_resolve)
    res = await ih.handle_issue_reopened(
        _payload("reopened", _issue(assignees=[{"login": "Blasted-ctrl"}])), db_session
    )
    assert attempts == ["99", "8"]
    assert res["status"] == "8"
