"""Unit tests for Plaky QA status option resolution from normalized board schema."""

from __future__ import annotations

import pytest

from boardman.plaky.dynamic_qa_status import (
    discover_qa_assignee_field_key_from_normalized,
    resolve_github_user_to_plaky_user_id,
    resolve_plaky_status_patch,
)


@pytest.mark.asyncio
async def test_resolve_github_approve_prefers_qa_verified_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = {
        "fields": [
            {
                "name": "Status",
                "type": "status",
                "key": "status-1",
                "options": [
                    {"name": "To Do", "id": "a1"},
                    {"name": "In QA", "id": "a2"},
                    {"name": "QA Verified", "id": "want-this"},
                    {"name": "Approved (PM)", "id": "a4"},
                ],
            }
        ]
    }

    async def _preload(*_a, **_k):
        return normalized

    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status._load_normalized",
        _preload,
    )
    out = await resolve_plaky_status_patch("any-board", intent="github_pr_review_approved")
    assert out is not None
    assert out[0] == "status-1"
    assert out[1] == "want-this"


@pytest.mark.asyncio
async def test_resolve_changes_requested_prefers_qa_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = {
        "fields": [
            {
                "name": "Status",
                "type": "status",
                "key": "s1",
                "options": [
                    {"name": "In QA", "id": "x1"},
                    {"name": "QA Rejected", "id": "rej"},
                    {"name": "Changes Requested", "id": "x2"},
                ],
            }
        ]
    }

    async def _preload(*_a, **_k):
        return normalized

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status._load_normalized", _preload)
    out = await resolve_plaky_status_patch("b", intent="github_pr_review_changes_requested")
    assert out is not None
    assert out[1] == "rej"


@pytest.mark.asyncio
async def test_resolve_in_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    normalized = {
        "fields": [
            {
                "name": "Workflow",
                "type": "status",
                "key": "wf",
                "options": [{"name": "In QA", "id": "inq"}],
            }
        ]
    }

    async def _preload(*_a, **_k):
        return normalized

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status._load_normalized", _preload)
    out = await resolve_plaky_status_patch("b", intent="workflow_in_qa")
    assert out == ("wf", "inq")


def test_discover_qa_field_prefers_person_qa_column() -> None:
    normalized = {
        "fields": [
            {"name": "Status", "type": "status", "key": "st"},
            {"name": "QA", "type": "person", "key": "person-qa"},
            {"name": "Notes", "type": "text", "key": "n"},
        ]
    }
    assert discover_qa_assignee_field_key_from_normalized(normalized) == "person-qa"


@pytest.mark.asyncio
async def test_resolve_github_user_prefers_plaky_github_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [
        {"id": "by-github", "name": "Other", "email": "other@x.com", "github_login": "DevLogin"},
        {"id": "by-email", "name": "Dev Person", "email": "dev@company.com"},
    ]

    class _FakePlaky:
        async def list_workspace_users(self):
            return {"ok": True, "users": users}

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.PlakyClient", lambda: _FakePlaky())
    out = await resolve_github_user_to_plaky_user_id(
        {"login": "devlogin", "name": "Dev Person", "email": "dev@company.com"}
    )
    assert out == "by-github"


@pytest.mark.asyncio
async def test_resolve_github_user_fuzzy_when_no_github_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [
        {
            "id": "plaky-jane",
            "name": "Jane Smith",
            "email": "jane.smith@acme.com",
            "primaryEmail": "jane.smith@acme.com",
        }
    ]

    class _FakePlaky:
        async def list_workspace_users(self):
            return {"ok": True, "users": users}

    monkeypatch.setattr("boardman.plaky.dynamic_qa_status.PlakyClient", lambda: _FakePlaky())
    out = await resolve_github_user_to_plaky_user_id(
        {
            "login": "jsmith-acme",
            "name": "Jane Smith",
            "email": "jane.smith@acme.com",
        }
    )
    assert out == "plaky-jane"
