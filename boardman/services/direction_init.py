"""Create or update DIRECTION.md in a GitHub repo from the template."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from boardman.settings import settings


def _template_body(repo_full: str) -> str:
    root = Path(__file__).resolve().parents[2] / "docs" / "DIRECTION_TEMPLATE.md"
    if root.is_file():
        return root.read_text(encoding="utf-8").replace("{{REPO}}", repo_full)
    return f"# {repo_full} Direction\n\n## What This Repo Does\n\n## Current Phase\n\n## What Needs to Be Done\n\n## What's NOT In Scope\n"


async def init_direction_file(
    owner: str,
    repo: str,
    *,
    branch: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    if not settings.github_pat:
        return {"ok": False, "message": "GITHUB_PAT not configured"}

    repo_full = f"{owner}/{repo}"
    content = _template_body(repo_full)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {"Authorization": f"Bearer {settings.github_pat}", "Accept": "application/vnd.github+json"}
        meta_r = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        if meta_r.status_code != 200:
            return {"ok": False, "message": f"Repo metadata: HTTP {meta_r.status_code}"}
        default_branch = meta_r.json().get("default_branch") or "main"
        ref = branch or default_branch

        path = "DIRECTION.md"
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        r = await client.get(url, headers=headers, params={"ref": ref})
        sha: Optional[str] = None
        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha")
            if not force:
                return {
                    "ok": True,
                    "skipped": True,
                    "message": "DIRECTION.md already exists; use force to overwrite",
                    "url": data.get("html_url"),
                    "branch": ref,
                }
        elif r.status_code != 404:
            return {"ok": False, "message": f"GitHub GET failed: {r.status_code} {r.text[:300]}"}

        body: Dict[str, Any] = {
            "message": "Add DIRECTION.md via deepiri-boardman init",
            "content": b64,
            "branch": ref,
        }
        if sha:
            body["sha"] = sha

        put = await client.put(url, headers=headers, json=body)
        if put.status_code not in (200, 201):
            return {"ok": False, "message": f"GitHub PUT failed: {put.status_code} {put.text[:400]}"}

        out = put.json()
        file_url = None
        if isinstance(out.get("content"), dict):
            file_url = out["content"].get("html_url")
        return {"ok": True, "url": file_url, "branch": ref}
