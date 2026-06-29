"""Create DIRECTION.md in a GitHub repo via a reviewable PR."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
from pathlib import Path
from typing import Any


def _template_body(repo_full: str) -> str:
    root = Path(__file__).resolve().parents[2] / "docs" / "DIRECTION_TEMPLATE.md"
    if root.is_file():
        return root.read_text(encoding="utf-8").replace("{{REPO}}", repo_full)
    return f"# {repo_full} Direction\n\n## What This Repo Does\n\n## Current Phase\n\n## What Needs to Be Done\n\n## What's NOT In Scope\n"


async def _run_cmd(*argv: str, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    out = out_b.decode("utf-8", errors="replace").strip()
    err = err_b.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


async def init_direction_file(
    owner: str,
    repo: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    repo_full = f"{owner}/{repo}"
    content = _template_body(repo_full)
    pr_branch = "boardman/init-direction"

    rc, out, err = await _run_cmd("gh", "--version")
    if rc != 0:
        return {"ok": False, "message": f"GitHub CLI not available: {err or out}"}

    rc, out, err = await _run_cmd("gh", "auth", "status")
    if rc != 0:
        return {"ok": False, "message": f"GitHub CLI not authenticated: {err or out}"}

    rc, out, err = await _run_cmd("gh", "api", "user", "--jq", ".login")
    if rc != 0 or not out.strip():
        return {"ok": False, "message": f"Unable to determine signed-in GitHub user: {err or out}"}
    viewer_login = out.strip()

    rc, out, err = await _run_cmd("gh", "api", f"repos/{owner}/{repo}")
    if rc != 0:
        return {"ok": False, "message": f"Repo metadata via gh failed: {err or out}"}
    try:
        repo_meta = json.loads(out)
    except Exception:
        return {"ok": False, "message": "Failed to parse repo metadata from GitHub CLI"}
    can_push = bool((repo_meta.get("permissions") or {}).get("push"))
    if not can_push:
        return {
            "ok": False,
            "message": (
                f"Signed-in user '{viewer_login}' does not have write access to {repo_full}. "
                "Authenticate with an account that can push to this repository."
            ),
        }

    existing: dict[str, Any] | None = None
    rc, out, err = await _run_cmd(
        "gh", "api", f"repos/{owner}/{repo}/contents/DIRECTION.md?ref=main"
    )
    if rc == 0 and out:
        try:
            existing = json.loads(out)
        except Exception:
            existing = None
    elif rc != 0 and "404" not in (err or out):
        return {"ok": False, "message": f"GitHub GET failed: {err or out}"}

    if existing:
        if not force:
            return {
                "ok": True,
                "skipped": True,
                "message": "DIRECTION.md already exists; use force to overwrite",
                "url": existing.get("html_url"),
                "branch": "main",
            }
        remote_b64 = (existing.get("content") or "").replace("\n", "")
        try:
            remote_text = base64.b64decode(remote_b64).decode("utf-8")
        except Exception:
            remote_text = ""
        if remote_text == content:
            return {
                "ok": True,
                "skipped": True,
                "message": "DIRECTION.md already matches template",
                "url": existing.get("html_url"),
                "branch": "main",
            }

    workspace_root = Path(__file__).resolve().parents[2]
    repos_root = workspace_root / ".repos"
    repos_root.mkdir(parents=True, exist_ok=True)
    clone_dir = repos_root / f"{owner}-{repo}"

    try:
        rc, out, err = await _run_cmd("gh", "repo", "clone", repo_full, str(clone_dir))
        if rc != 0:
            return {"ok": False, "message": f"Clone failed: {err or out}"}

        rc, out, err = await _run_cmd("git", "fetch", "origin", "main", cwd=clone_dir)
        if rc != 0:
            return {"ok": False, "message": f"Fetch base branch failed: {err or out}"}

        rc, out, err = await _run_cmd(
            "git", "checkout", "-B", pr_branch, "origin/main", cwd=clone_dir
        )
        if rc != 0:
            return {"ok": False, "message": f"Create branch failed: {err or out}"}

        (clone_dir / "DIRECTION.md").write_text(content, encoding="utf-8")

        rc, out, err = await _run_cmd("git", "add", "DIRECTION.md", cwd=clone_dir)
        if rc != 0:
            return {"ok": False, "message": f"git add failed: {err or out}"}

        rc, out, err = await _run_cmd("git", "diff", "--cached", "--quiet", cwd=clone_dir)
        if rc == 0:
            return {
                "ok": True,
                "skipped": True,
                "message": "No changes to commit for DIRECTION.md",
                "branch": "main",
            }

        rc, out, err = await _run_cmd(
            "git", "commit", "-m", "Add DIRECTION.md via deepiri-boardman init", cwd=clone_dir
        )
        if rc != 0:
            return {
                "ok": False,
                "message": (
                    f"git commit failed: {err or out}. "
                    "Configure your git identity (`git config user.name/user.email`) and retry."
                ),
            }

        rc, out, err = await _run_cmd("git", "push", "-u", "origin", pr_branch, cwd=clone_dir)
        if rc != 0:
            return {"ok": False, "message": f"git push failed: {err or out}"}

        pr_title = "Add DIRECTION.md via deepiri-boardman init"
        pr_body = (
            "This PR initializes `DIRECTION.md` from the deepiri-boardman template so the repo "
            "has a clear direction document for planning and scan workflows."
            f"Ensure that the branch '{pr_branch}' is deleted after the PR is merged."
        )
        rc, out, err = await _run_cmd(
            "gh",
            "pr",
            "create",
            "--repo",
            repo_full,
            "--base",
            "main",
            "--head",
            pr_branch,
            "--title",
            pr_title,
            "--body",
            pr_body,
        )
        if rc != 0:
            return {"ok": False, "message": f"gh pr create failed: {err or out}"}

        return {
            "ok": True,
            "url": out.strip(),
            "branch": "main",
            "pr_branch": pr_branch,
            "actor": viewer_login,
        }
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)
