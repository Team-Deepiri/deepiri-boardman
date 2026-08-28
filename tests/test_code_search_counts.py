"""Defect-scan counting honesty: per-file counts, no cross-file misattribution."""

from __future__ import annotations

import httpx
import pytest

from boardman.github import code_search as cs


def _tree(paths_sizes: list[tuple[str, int]]) -> dict:
    return {
        "truncated": False,
        "tree": [{"path": p, "type": "blob", "size": s} for p, s in paths_sizes],
    }


def _transport(files: dict[str, str]) -> httpx.MockTransport:
    import base64

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/r"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/trees/" in path:
            return httpx.Response(200, json=_tree([(p, len(t)) for p, t in files.items()]))
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            text = files.get(name, "")
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()},
            )
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_counts_are_per_file_not_a_cross_file_total(monkeypatch) -> None:
    """A cross-file total shown beside single-file examples was read as that file's count
    ("56 occurrences in X" when X had 46). Counts must be attributable per file."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = {
        "a.py": "try:\n    x()\nexcept Exception:\n    pass\n" * 3,  # 3 hits
        "b.py": "try:\n    y()\nexcept Exception:\n    pass\n" * 5,  # 5 hits
    }
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await cs.scan_repo_defects(c, "o", "r")

    assert out is not None
    broad = next(f for f in out["findings"] if f["probe"] == "broad_except")
    assert broad["occurrences_by_file"] == {"a.py": 3, "b.py": 5}
    assert broad["total_across_files_read"] == 8
    # Examples must span BOTH files, not four hits from one.
    assert {e["path"] for e in broad["examples"]} == {"a.py", "b.py"}


@pytest.mark.asyncio
async def test_sleep_probe_does_not_assert_an_async_call_path(monkeypatch) -> None:
    """time.sleep in a sync-only helper is not an event-loop bug. The probe text must not
    assert that it is — the model repeats these strings verbatim."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    files = {"s.py": "import time\ntime.sleep(1)\n"}
    async with httpx.AsyncClient(transport=_transport(files)) as c:
        out = await cs.scan_repo_defects(c, "o", "r")

    sleep = next(f for f in out["findings"] if f["probe"] == "blocking_sleep")
    means = sleep["means"].lower()
    assert "only a defect if" in means and "check the caller" in means
    assert "freezes the event loop in async code" not in means


@pytest.mark.asyncio
async def test_coverage_note_admits_truncation(monkeypatch) -> None:
    """Counts are lower bounds when files are truncated; the note must say so."""
    import boardman.settings as bs

    monkeypatch.setattr(bs.settings, "github_pat", "t")
    async with httpx.AsyncClient(transport=_transport({"a.py": "except Exception:\n"})) as c:
        out = await cs.scan_repo_defects(c, "o", "r")
    assert "at least" in out["coverage_note"].lower()
