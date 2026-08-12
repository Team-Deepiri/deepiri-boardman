"""Reading a PR for a merge decision: absence of data must never read as evidence.

The assistant used to answer "is #77 safe to merge?" from the title plus a repo-wide
defect scan, because it had no way to open a PR. Now that it can, the failure mode
shifts: a reviews call that 403s must not render as "nobody has reviewed it", and a
stale APPROVED must not outrank the CHANGES_REQUESTED that followed it.
"""

from __future__ import annotations

import httpx
import pytest

from boardman.github import pr_review_context as prc

_PR = {
    "number": 77,
    "title": "feat(qa): PR-time QA assignment",
    "user": {"login": "Blasted-ctrl"},
    "state": "open",
    "draft": False,
    "merged": False,
    "mergeable": True,
    "mergeable_state": "blocked",
    "base": {"ref": "main"},
    "head": {"ref": "ali_f/feat/qa", "sha": "deadbeef"},
    "additions": 6010,
    "deletions": 116,
    "changed_files": 50,
    "commits": 25,
    "body": "Fixes #78 and refs #49",
    "html_url": "https://github.com/o/r/pull/77",
}


def _transport(
    *,
    reviews_status: int = 200,
    reviews: list | None = None,
    checks: dict | None = None,
    files_status: int = 200,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/pulls/77"):
            return httpx.Response(200, json=_PR)
        if p.endswith("/pulls/77/files"):
            if files_status != 200:
                return httpx.Response(files_status, json={})
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "boardman/services/pr_handler.py",
                        "status": "modified",
                        "additions": 40,
                        "deletions": 3,
                    }
                ],
            )
        if p.endswith("/pulls/77/reviews"):
            return httpx.Response(reviews_status, json=reviews if reviews is not None else [])
        if "/check-runs" in p:
            return httpx.Response(200, json=checks or {"total_count": 0, "check_runs": []})
        if p.endswith("/pulls/77/commits"):
            return httpx.Response(200, json=[{"commit": {"message": "fix: guard retry\n\nbody"}}])
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


async def _ctx(monkeypatch, **kw) -> dict:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    async with httpx.AsyncClient(transport=_transport(**kw)) as c:
        return await prc.fetch_pull_request_context("o/r", 77, client=c)


@pytest.mark.asyncio
async def test_reads_the_facts_a_merge_decision_rests_on(monkeypatch) -> None:
    ctx = await _ctx(monkeypatch)
    assert ctx["ok"] and ctx["author"] == "Blasted-ctrl"
    assert ctx["mergeable_state"] == "blocked"
    assert (ctx["additions"], ctx["deletions"], ctx["changed_files_total"]) == (6010, 116, 50)
    assert ctx["linked_issues"] == [78] and ctx["mentioned_issues"] == [49]
    assert ctx["files_truncated"] is True  # 1 of 50 returned
    text = prc.render_pull_request_context(ctx)
    assert "list truncated" in text
    assert "guard retry" in text and "body" not in text.split("commits:")[1]


@pytest.mark.asyncio
async def test_unavailable_reviews_are_not_reported_as_no_reviews(monkeypatch) -> None:
    ctx = await _ctx(monkeypatch, reviews_status=403)
    text = prc.render_pull_request_context(ctx)
    assert "reviews: UNAVAILABLE" in text
    assert "none submitted" not in text
    assert "PAT lacks read access" in text


@pytest.mark.asyncio
async def test_latest_review_verdict_wins_over_stale_approval(monkeypatch) -> None:
    reviews = [
        {"user": {"login": "austin"}, "state": "APPROVED"},
        {"user": {"login": "austin"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "em"}, "state": "APPROVED"},
        {"user": {"login": "em"}, "state": "COMMENTED"},  # a comment does not undo an approval
    ]
    ctx = await _ctx(monkeypatch, reviews=reviews)
    assert ctx["reviews"] == {"austin": "CHANGES_REQUESTED", "em": "APPROVED"}


@pytest.mark.asyncio
async def test_failing_and_pending_checks_are_named(monkeypatch) -> None:
    checks = {
        "total_count": 3,
        "check_runs": [
            {"name": "pytest", "status": "completed", "conclusion": "failure"},
            {"name": "ruff", "status": "completed", "conclusion": "success"},
            {"name": "build", "status": "in_progress", "conclusion": None},
        ],
    }
    ctx = await _ctx(monkeypatch, checks=checks)
    assert ctx["checks"]["failing"] == ["pytest (failure)"]
    assert ctx["checks"]["passing"] == 1 and ctx["checks"]["pending"] == ["build"]
    assert "FAILING: pytest (failure)" in prc.render_pull_request_context(ctx)


@pytest.mark.asyncio
async def test_missing_pr_says_so_instead_of_returning_a_shell(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ctx = await prc.fetch_pull_request_context("o/r", 99999, client=c)
    assert ctx["ok"] is False
    assert "not found" in prc.render_pull_request_context(ctx)


@pytest.mark.asyncio
async def test_no_pat_is_an_error_not_an_empty_pr(monkeypatch) -> None:
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "")
    ctx = await prc.fetch_pull_request_context("o/r", 77)
    assert ctx["ok"] is False and "GITHUB_PAT" in ctx["error"]
