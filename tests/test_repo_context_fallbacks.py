"""Repo-explanation fallbacks: no-docs repos must still produce usable context."""

from __future__ import annotations

import json

import httpx
import pytest

from boardman.agent.tools import github_tools as gt
from boardman.agent.tools.repo_tools import _scan_local_repo
from boardman.github.repo_fetch import fetch_repo_overview


def test_scan_local_repo_falls_back_to_structure(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "no-docs-app"}', encoding="utf-8")

    out = json.loads(_scan_local_repo(str(tmp_path)))
    assert out["ok"] is True
    assert out["files"] == []
    assert "structure_summary" in out and out["structure_summary"]
    assert any(m["path"].lower() == "package.json" for m in out["manifests"])
    assert "No README" in out["message"]


def test_scan_local_repo_still_prefers_docs(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Hello", encoding="utf-8")
    out = json.loads(_scan_local_repo(str(tmp_path)))
    assert out["files"] and out["files"][0]["path"].lower() == "readme.md"
    assert "structure_summary" not in out


def test_looks_missing_detects_placeholder_strings() -> None:
    assert gt._looks_missing("(No DIRECTION.md found or inaccessible: HTTP 404)")
    assert gt._looks_missing("")
    assert not gt._looks_missing("# Real content")


@pytest.mark.asyncio
async def test_fetch_repo_overview_summarizes_tree() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/o/r":
            return httpx.Response(
                200,
                json={
                    "description": "A demo service",
                    "topics": ["fastapi"],
                    "default_branch": "develop",
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "archived": False,
                },
            )
        if path == "/repos/o/r/languages":
            return httpx.Response(200, json={"Python": 8000, "Dockerfile": 2000})
        if path == "/repos/o/r/git/trees/develop":
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"path": "app/main.py", "type": "blob"},
                        {"path": "app/api.py", "type": "blob"},
                        {"path": "pyproject.toml", "type": "blob"},
                        {"path": "Dockerfile", "type": "blob"},
                    ],
                },
            )
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await fetch_repo_overview(client, "o", "r")

    assert out["description"] == "A demo service"
    assert out["default_branch"] == "develop"
    assert out["languages"]["Python"] == 80.0
    assert "pyproject.toml" in out["manifests"] and "Dockerfile" in out["manifests"]
    assert "app/main.py" in out["entry_points"]
    assert out["top_level"]["app"] == 2


@pytest.mark.asyncio
async def test_planning_context_guides_when_repo_has_no_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_direction(client, owner, repo):
        return "(No DIRECTION.md found or inaccessible: HTTP 404)"

    async def no_readme(client, owner, repo):
        return "(No README found or inaccessible: HTTP 404)"

    async def overview(client, owner, repo):
        return {
            "full_name": f"{owner}/{repo}",
            "description": "agent runtime",
            "default_branch": "main",
            "manifests": ["pyproject.toml"],
            "top_level": {"cyrex": 40},
            "entry_points": ["cyrex/main.py"],
        }

    async def commits(client, owner, repo, limit=20):
        return "- abc123 initial"

    async def issues(client, owner, repo):
        return "(no open issues)"

    async def file_text(client, owner, repo, path, *, ref=""):
        return "[tool.poetry]\nname = 'cyrex'"

    monkeypatch.setattr(gt, "fetch_direction_md", no_direction)
    monkeypatch.setattr(gt, "fetch_readme_md", no_readme)
    monkeypatch.setattr(gt, "fetch_repo_overview", overview)
    monkeypatch.setattr(gt, "fetch_recent_commits", commits)
    monkeypatch.setattr(gt, "fetch_open_issues", issues)
    monkeypatch.setattr(gt, "fetch_repo_file_text", file_text)
    monkeypatch.setattr(gt.settings, "github_pat", "x" * 8)

    raw = await gt._github_repo_planning_context("Team-Deepiri/deepiri-cyrex")
    out = json.loads(raw)
    assert out["ok"] is True
    assert out["context_sources"]["DIRECTION_md"] == "missing"
    assert out["context_sources"]["README"] == "missing"
    assert out["context_sources"]["structure_overview"] == "found"
    assert "Do NOT give up" in out["guidance"]
    assert out["manifest_excerpts"]["pyproject.toml"].startswith("[tool.poetry]")
    assert out["structure_overview"]["entry_points"] == ["cyrex/main.py"]
