"""Workflow intent <-> ClickUp status name mapping."""

from __future__ import annotations

import httpx

from boardman.clickup import statuses
from boardman.clickup.client import ClickUpClient


def _set(monkeypatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setattr(statuses.settings, f"clickup_status_{name}", value)


def test_intent_to_status_uses_settings_and_empty_means_not_written(monkeypatch):
    _set(monkeypatch, assigned="Assigned", completed="Done", needs_qa="")
    assert statuses.status_for_intent("workflow_assigned") == "Assigned"
    assert statuses.status_for_intent("workflow_completed") == "Done"
    assert statuses.status_for_intent("workflow_needs_qa") == ""
    assert statuses.status_for_intent("not_an_intent") == ""


def test_reverse_lookup_is_case_insensitive_and_unknown_is_empty(monkeypatch):
    _set(monkeypatch, in_progress="In Progress", completed="Done")
    assert statuses.intent_for_status("in progress") == "workflow_in_progress"
    assert statuses.intent_for_status("  DONE ") == "workflow_completed"
    assert statuses.intent_for_status("some custom status") == ""
    assert statuses.intent_for_status("") == ""


def test_shared_name_resolves_to_the_earlier_intent(monkeypatch):
    _set(monkeypatch, needs_assigned="to do", assigned="to do")
    assert statuses.intent_for_status("to do") == "workflow_needs_assigned"


def test_an_unset_status_never_matches_an_empty_current_status(monkeypatch):
    _set(monkeypatch, paused="", needs_qa="")
    assert statuses.intent_for_status("") == ""


async def test_tag_calls_encode_the_name_and_report_failures():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.raw_path.decode()))
        return (
            httpx.Response(404, text="no such task")
            if "bad" in str(req.url)
            else httpx.Response(200, json={})
        )

    c = ClickUpClient("tok", "https://cu.test/api/v2", transport=httpx.MockTransport(handler))
    assert (await c.add_tag("t1", "type:bug"))["ok"]
    assert (await c.remove_tag("t1", "a b/c"))["ok"]
    assert seen[0] == ("POST", "/api/v2/task/t1/tag/type%3Abug")
    assert seen[1] == ("DELETE", "/api/v2/task/t1/tag/a%20b%2Fc")
    r = await c.add_tag("bad", "x")
    assert r["ok"] is False and r["status"] == 404
    assert (await ClickUpClient("", "https://cu.test").add_tag("t", "x"))["status"] == 400


async def test_create_task_sends_tags():
    sent = {}

    def handler(req):
        import json

        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"id": "1"})

    c = ClickUpClient(
        "tok", "https://cu.test/api/v2", default_list_id="L", transport=httpx.MockTransport(handler)
    )
    await c.create_task("t", "d", "very important", tags=["repo", "type:bug"])
    assert sent["tags"] == ["repo", "type:bug"] and sent["priority"] == 1
