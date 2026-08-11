from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardman.services import direction_init as di


@pytest.mark.asyncio
async def test_init_direction_requires_push_access(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_cmd(*argv: str, cwd: Path | None = None):
        calls.append(argv)
        if argv[:2] == ("gh", "--version"):
            return 0, "gh version 2.x", ""
        if argv[:3] == ("gh", "auth", "status"):
            return 0, "ok", ""
        if argv[:4] == ("gh", "api", "user", "--jq"):
            return 0, "alice", ""
        if argv[:3] == ("gh", "api", "repos/acme/demo"):
            return 0, json.dumps({"default_branch": "main", "permissions": {"push": False}}), ""
        return 1, "", "unexpected command"

    monkeypatch.setattr(di, "_run_cmd", fake_run_cmd)

    res = await di.init_direction_file("acme", "demo")

    assert res["ok"] is False
    assert "does not have write access" in str(res.get("message", ""))
    assert all(c[:3] != ("gh", "repo", "clone") for c in calls)


@pytest.mark.asyncio
async def test_init_direction_skips_when_existing_without_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_cmd(*argv: str, cwd: Path | None = None):
        if argv[:2] == ("gh", "--version"):
            return 0, "gh version 2.x", ""
        if argv[:3] == ("gh", "auth", "status"):
            return 0, "ok", ""
        if argv[:4] == ("gh", "api", "user", "--jq"):
            return 0, "alice", ""
        if argv[:3] == ("gh", "api", "repos/acme/demo"):
            return 0, json.dumps({"default_branch": "main", "permissions": {"push": True}}), ""
        if argv[:2] == ("gh", "api") and "contents/DIRECTION.md?ref=main" in argv[2]:
            return (
                0,
                json.dumps({"html_url": "https://github.com/acme/demo/blob/main/DIRECTION.md"}),
                "",
            )
        return 1, "", "unexpected command"

    monkeypatch.setattr(di, "_run_cmd", fake_run_cmd)

    res = await di.init_direction_file("acme", "demo", force=False)

    assert res["ok"] is True
    assert res["skipped"] is True
    assert "already exists" in str(res.get("message", ""))


@pytest.mark.asyncio
async def test_init_direction_creates_pr_with_user_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_cmd(*argv: str, cwd: Path | None = None):
        calls.append(argv)
        if argv[:2] == ("gh", "--version"):
            return 0, "gh version 2.x", ""
        if argv[:3] == ("gh", "auth", "status"):
            return 0, "ok", ""
        if argv[:4] == ("gh", "api", "user", "--jq"):
            return 0, "alice", ""
        if argv[:3] == ("gh", "api", "repos/acme/demo"):
            return 0, json.dumps({"default_branch": "main", "permissions": {"push": True}}), ""
        if argv[:2] == ("gh", "api") and "contents/DIRECTION.md?ref=main" in argv[2]:
            return 1, "", "gh: Not Found (HTTP 404)"
        if argv[:3] == ("gh", "repo", "clone"):
            Path(argv[4]).mkdir(parents=True, exist_ok=True)
            return 0, "", ""
        if argv[:2] == ("git", "fetch"):
            return 0, "", ""
        if argv[:2] == ("git", "checkout"):
            return 0, "", ""
        if argv[:3] == ("git", "add", "DIRECTION.md"):
            return 0, "", ""
        if argv[:4] == ("git", "diff", "--cached", "--quiet"):
            return 1, "", ""  # staged changes present
        if argv[:2] == ("git", "commit"):
            return 0, "[main abc123] Add DIRECTION.md", ""
        if argv[:2] == ("git", "push"):
            return 0, "", ""
        if argv[:3] == ("gh", "pr", "create"):
            return 0, "https://github.com/acme/demo/pull/42", ""
        return 1, "", f"unexpected command: {' '.join(argv)}"

    monkeypatch.setattr(di, "_run_cmd", fake_run_cmd)

    res = await di.init_direction_file("acme", "demo")

    assert res["ok"] is True
    assert res["actor"] == "alice"
    assert str(res["url"]).endswith("/pull/42")
    assert str(res["pr_branch"]) == "boardman/init-direction"
    commit_calls = [c for c in calls if c[:2] == ("git", "commit")]
    assert commit_calls, "expected git commit to be called"
    assert "user.name=deepiri-boardman" not in " ".join(commit_calls[0])
