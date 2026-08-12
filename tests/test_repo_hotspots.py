"""Code-level repo signals: largest files, test ratio, committed-artifact smells."""

from __future__ import annotations

import httpx
import pytest

from boardman.github.repo_hotspots import fetch_repo_hotspots


def _tree_response(paths_sizes: list[tuple[str, int]]) -> dict:
    return {
        "truncated": False,
        "tree": [{"path": p, "type": "blob", "size": s} for p, s in paths_sizes],
    }


def _transport(paths_sizes: list[tuple[str, int]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deepiri-boardman"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json=_tree_response(paths_sizes))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_hotspots_ranks_largest_source_and_counts_tests(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = [
        ("boardman/plaky/client.py", 70000),
        ("boardman/main.py", 3500),
        ("tests/test_client.py", 5000),
        ("tests/test_main.py", 4000),
        ("README.md", 900),
        ("node_modules/dep/index.js", 999999),  # vendor must be ignored
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    assert out["source_files"] == 2  # client.py + main.py (tests counted separately)
    assert out["test_files"] == 2
    assert out["test_to_source_ratio"] == 1.0
    top = out["largest_source_files"][0]
    assert top["path"] == "boardman/plaky/client.py"
    # Size only — never a fabricated line count (bytes/35 was 25%+ off real LOC).
    assert top["size_kb"] > 60
    assert "approx_lines" not in top
    assert "line" in out["size_note"].lower()
    assert all("node_modules" not in f["path"] for f in out["largest_source_files"])


@pytest.mark.asyncio
async def test_hotspots_flags_committed_secrets_and_databases(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = [
        (".env", 800),
        ("boardman.db", 40000),
        ("app/id_rsa", 1600),
        (".env.example", 700),  # templates are fine
        ("app/main.py", 1200),
    ]
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await fetch_repo_hotspots(c, "Team-Deepiri", "deepiri-boardman")

    assert out is not None
    flagged = {a["path"] for a in out["tracked_artifacts"]}
    assert ".env" in flagged and "boardman.db" in flagged and "app/id_rsa" in flagged
    assert ".env.example" not in flagged


@pytest.mark.asyncio
async def test_hotspots_returns_none_without_a_token(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "")
    async with httpx.AsyncClient(transport=_transport([])) as c:
        assert await fetch_repo_hotspots(c, "o", "r") is None


# --- Plaky list projection: a partial board must never look complete -----------------


def test_task_envelope_reports_counts_and_never_silently_truncates() -> None:
    from boardman.agent.tools.plaky_tools import _envelope

    items = [{"id": i, "title": f"task {i}", "status": "NEEDS ASSIGNED"} for i in range(100)]
    import json

    out = json.loads(_envelope({"ok": True, "message": ""}, items, limit=60))
    assert out["returned"] == 60 and out["total"] == 100 and out["truncated"] is True
    assert "60 of 100" in out["note"]
    # Must still be VALID json (the old char-slice cut mid-object).
    assert len(out["tasks"]) == 60


def test_task_projection_keeps_status_and_assignees_only() -> None:
    from boardman.agent.tools.plaky_tools import _slim_task

    slim = _slim_task(
        {
            "id": 7,
            "title": "Fix retry crash",
            "fields": [
                {"key": "status-6", "type": "STATUS", "title": "Status", "value": "In QA"},
                {
                    "key": "person-4",
                    "type": "PERSON",
                    "title": "QA Engineer Assigned",
                    "value": {"assignedUsers": [476634]},
                },
            ],
        }
    )
    assert slim["id"] == 7 and slim["title"] == "Fix retry crash"
    assert slim["status"] == "In QA"
    assert slim["assignees"][0]["users"] == [476634]
    assert "fields" not in slim  # the bulk that used to blow the size budget
