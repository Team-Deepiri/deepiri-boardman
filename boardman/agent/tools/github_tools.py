"""GitHub read-only helpers for the agent."""

from __future__ import annotations

import json

import httpx
from langchain_core.tools import StructuredTool

from boardman.settings import settings


async def _github_list_open_issues(owner_repo: str) -> str:
    if not settings.github_pat:
        return json.dumps({"ok": False, "message": "GITHUB_PAT not configured"})
    if "/" not in owner_repo:
        return json.dumps({"ok": False, "message": "owner_repo must be owner/name"})
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
        r = await client.get(
            f"https://api.github.com/repos/{owner_repo}/issues?state=open&per_page=30",
            headers=headers,
        )
        if r.status_code != 200:
            return json.dumps({"ok": False, "status": r.status_code, "text": r.text[:500]})
        issues = r.json()
        slim = [
            {"number": i["number"], "title": i.get("title"), "url": i.get("html_url")}
            for i in issues
            if "pull_request" not in i
        ]
        return json.dumps({"ok": True, "issues": slim})


def github_list_open_issues_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_github_list_open_issues,
        name="github_list_open_issues",
        description="List open GitHub issues (not PRs) for owner/repo (e.g. deepiri-org/boardman).",
    )
