"""Read a pull request the way a reviewer does: body, changed files, reviews, CI.

The assistant could list PRs but never open one, so "is #77 safe to merge?" fell back
to guessing from the title plus a whole-repo defect scan — a defensible answer about
the wrong thing. This pulls the evidence a merge decision actually rests on.

Every section records its own failure. A reviews call that 403s must not read back as
"no reviews yet", so each block is either data or an explicit error string, and the
caller renders the difference.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from boardman.github.repo_fetch import _parse_owner_repo
from boardman.settings import settings

_MAX_FILES = 40  # fallback; settings.github_pr_max_files wins when set
_MAX_BODY_CHARS = 4000  # fallback; settings.github_pr_max_body_chars wins when set
# GitHub's actual closing keywords. "refs #12" links an issue without closing it, so it
# belongs in mentioned_issues — claiming a PR closes an issue it merely cites is a lie
# a reviewer would act on.
_LINKED_RE = re.compile(
    r"(?:^|\s)(?:closes|close|closed|fixes|fix|fixed|resolves|resolve|resolved)\s+#(\d+)",
    re.I,
)
_BARE_ISSUE_RE = re.compile(r"(?<![\w/])#(\d+)")


def _err(status: int, what: str) -> str:
    if status == 404:
        return f"{what}: not found (wrong number, or the PAT cannot see this repo)"
    if status in (401, 403):
        return f"{what}: {status} — the PAT lacks read access to this repo's pull requests"
    return f"{what}: GitHub returned {status}"


async def _json(client: httpx.AsyncClient, path: str, what: str) -> tuple[Any, str]:
    """Returns (data, error). Exactly one is truthy."""
    try:
        r = await client.get(
            f"https://api.github.com{path}",
            headers={
                "Authorization": f"Bearer {settings.github_pat}",
                "Accept": "application/vnd.github+json",
            },
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        return None, f"{what}: request failed ({type(e).__name__})"
    if r.status_code != 200:
        return None, _err(r.status_code, what)
    try:
        return r.json(), ""
    except ValueError:
        return None, f"{what}: unexpected (non-JSON) response"


def _login(block: Any) -> str:
    return str((block or {}).get("login") or "").strip() if isinstance(block, dict) else ""


async def fetch_pull_request_context(
    full_name: str,
    pr_number: int,
    *,
    max_files: int = 0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Everything needed to judge a PR, with per-section errors preserved."""
    if not max_files:
        max_files = int(getattr(settings, "github_pr_max_files", 0) or _MAX_FILES)
    parsed = _parse_owner_repo(full_name)
    if not parsed:
        return {"ok": False, "error": f"{full_name!r} is not in owner/repo form"}
    if not (settings.github_pat or "").strip():
        return {"ok": False, "error": "GITHUB_PAT is not configured — cannot read pull requests"}
    owner, repo = parsed
    base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    n = int(pr_number)

    # Injected client (tests) is honored; otherwise the shared keep-alive pool.
    # github_http_client() returns the SHARED instance bound to the current event loop.
    # It is managed by the lifespan shutdown (aclose_shared_http_clients) and must never
    # be closed here; owns_client stays False so the finally block skips it.
    owns_client = False
    if client is None:
        from boardman.github.http import github_http_client

        client = github_http_client()
    try:
        pr, pr_err = await _json(client, f"{base}/pulls/{n}", "pull request")
        if pr_err or not isinstance(pr, dict):
            return {"ok": False, "error": pr_err or "pull request: unexpected response"}

        files, files_err = await _json(
            client, f"{base}/pulls/{n}/files?per_page={max_files}", "changed files"
        )
        reviews, reviews_err = await _json(
            client, f"{base}/pulls/{n}/reviews?per_page=100", "reviews"
        )

        head_sha = str(((pr.get("head") or {}) or {}).get("sha") or "")
        checks: Any = None
        checks_err = ""
        if head_sha:
            checks, checks_err = await _json(
                client, f"{base}/commits/{head_sha}/check-runs?per_page=100", "CI checks"
            )
        else:
            checks_err = "CI checks: PR head sha missing from the API response"

        commits, commits_err = await _json(
            client, f"{base}/pulls/{n}/commits?per_page=100", "commits"
        )
    finally:
        if owns_client:
            await client.aclose()

    body = str(pr.get("body") or "").strip()
    max_body = int(getattr(settings, "github_pr_max_body_chars", 0) or _MAX_BODY_CHARS)
    body_truncated = len(body) > max_body
    if body_truncated:
        body = body[:max_body]

    file_rows: list[dict[str, Any]] = []
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict):
                file_rows.append(
                    {
                        "filename": str(f.get("filename") or ""),
                        "status": str(f.get("status") or ""),
                        "additions": int(f.get("additions") or 0),
                        "deletions": int(f.get("deletions") or 0),
                    }
                )

    # Latest non-COMMENTED verdict per reviewer — an APPROVED that was later superseded by
    # CHANGES_REQUESTED must not be reported as an approval.
    latest: dict[str, str] = {}
    if isinstance(reviews, list):
        for rv in reviews:
            if not isinstance(rv, dict):
                continue
            who = _login(rv.get("user"))
            state = str(rv.get("state") or "").upper()
            if not who or state in ("", "PENDING"):
                continue
            if state == "COMMENTED" and who in latest:
                continue
            latest[who] = state

    check_summary: dict[str, Any] = {}
    if isinstance(checks, dict):
        runs = checks.get("check_runs")
        failing: list[str] = []
        pending: list[str] = []
        passing = 0
        if isinstance(runs, list):
            for run in runs:
                if not isinstance(run, dict):
                    continue
                name = str(run.get("name") or "")
                concl = str(run.get("conclusion") or "").lower()
                status = str(run.get("status") or "").lower()
                if status != "completed":
                    pending.append(name)
                elif concl in ("success", "neutral", "skipped"):
                    passing += 1
                else:
                    failing.append(f"{name} ({concl or 'no conclusion'})")
        check_summary = {
            "total": int(checks.get("total_count") or 0),
            "passing": passing,
            "failing": failing,
            "pending": pending,
        }

    text = f"{pr.get('title') or ''}\n{body}"
    linked = sorted({int(m) for m in _LINKED_RE.findall(text)})
    mentioned = sorted({int(m) for m in _BARE_ISSUE_RE.findall(text) if int(m) != n})

    return {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "number": n,
        "title": str(pr.get("title") or ""),
        "author": _login(pr.get("user")),
        "state": str(pr.get("state") or ""),
        "draft": bool(pr.get("draft")),
        "merged": bool(pr.get("merged")),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": str(pr.get("mergeable_state") or ""),
        "base": str(((pr.get("base") or {}) or {}).get("ref") or ""),
        "head": str(((pr.get("head") or {}) or {}).get("ref") or ""),
        "created_at": str(pr.get("created_at") or ""),
        "updated_at": str(pr.get("updated_at") or ""),
        "additions": int(pr.get("additions") or 0),
        "deletions": int(pr.get("deletions") or 0),
        "changed_files_total": int(pr.get("changed_files") or 0),
        "commits_total": int(pr.get("commits") or 0),
        "body": body,
        "body_truncated": body_truncated,
        "body_empty": not body,
        "files": file_rows,
        "files_truncated": len(file_rows) < int(pr.get("changed_files") or 0),
        "files_error": files_err,
        "reviews": latest,
        "reviews_error": reviews_err,
        "requested_reviewers": sorted(
            filter(None, (_login(u) for u in (pr.get("requested_reviewers") or [])))
        ),
        "checks": check_summary,
        "checks_error": checks_err,
        "commit_subjects": [
            str(((c.get("commit") or {}) or {}).get("message") or "").splitlines()[0][:120]
            for c in (commits if isinstance(commits, list) else [])
            if isinstance(c, dict)
        ][:30],
        "commits_error": commits_err,
        "linked_issues": linked,
        "mentioned_issues": [i for i in mentioned if i not in linked],
        "html_url": str(pr.get("html_url") or ""),
    }


def render_pull_request_context(ctx: dict[str, Any]) -> str:
    """Compact text for the LLM. Errors stay visible so absence is never read as evidence."""
    if not ctx.get("ok"):
        return f"Cannot read this pull request — {ctx.get('error')}"

    out: list[str] = []
    state = "merged" if ctx["merged"] else ctx["state"]
    draft = " [DRAFT]" if ctx["draft"] else ""
    out.append(f"PR #{ctx['number']} in {ctx['repo']}: {ctx['title']}{draft}")
    out.append(
        f"author: {ctx['author'] or 'unknown'} | state: {state} | {ctx['head']} -> {ctx['base']}"
    )
    out.append(
        f"size: +{ctx['additions']}/-{ctx['deletions']} across {ctx['changed_files_total']} "
        f"file(s), {ctx['commits_total']} commit(s)"
    )
    if ctx["mergeable_state"]:
        out.append(f"mergeable_state: {ctx['mergeable_state']} (mergeable={ctx['mergeable']})")

    if ctx["body_empty"]:
        out.append("\ndescription: (empty — the author wrote no PR body)")
    else:
        trunc = " …[truncated]" if ctx["body_truncated"] else ""
        out.append(f"\ndescription:\n{ctx['body']}{trunc}")

    if ctx["linked_issues"]:
        out.append("\ncloses issues: " + ", ".join(f"#{i}" for i in ctx["linked_issues"]))
    if ctx["mentioned_issues"]:
        out.append("mentions issues: " + ", ".join(f"#{i}" for i in ctx["mentioned_issues"]))

    if ctx["files_error"]:
        out.append(f"\nchanged files: UNAVAILABLE — {ctx['files_error']}")
    elif ctx["files"]:
        shown = "\n".join(
            f"  {f['status']:<9} +{f['additions']}/-{f['deletions']}  {f['filename']}"
            for f in ctx["files"]
        )
        more = " (list truncated)" if ctx["files_truncated"] else ""
        out.append(f"\nchanged files{more}:\n{shown}")

    if ctx["reviews_error"]:
        out.append(f"\nreviews: UNAVAILABLE — {ctx['reviews_error']}")
    elif ctx["reviews"]:
        out.append(
            "\nreviews: "
            + ", ".join(f"{who}={state}" for who, state in sorted(ctx["reviews"].items()))
        )
    else:
        out.append("\nreviews: none submitted yet")
    if ctx["requested_reviewers"]:
        out.append("review requested from: " + ", ".join(ctx["requested_reviewers"]))

    if ctx["checks_error"]:
        out.append(f"CI: UNAVAILABLE — {ctx['checks_error']}")
    elif ctx["checks"]:
        c = ctx["checks"]
        if not c["total"]:
            out.append("CI: no checks configured or none have run on the head commit")
        else:
            bits = [f"{c['passing']} passing"]
            if c["failing"]:
                bits.append(f"FAILING: {', '.join(c['failing'][:8])}")
            if c["pending"]:
                bits.append(f"{len(c['pending'])} still running")
            out.append("CI: " + " | ".join(bits))

    if ctx["commits_error"]:
        out.append(f"commits: UNAVAILABLE — {ctx['commits_error']}")
    elif ctx["commit_subjects"]:
        out.append("commits:\n" + "\n".join(f"  - {s}" for s in ctx["commit_subjects"]))

    return "\n".join(out)
