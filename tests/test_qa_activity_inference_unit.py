"""QA tier from PR search activity (mocked HTTP, no GitHub)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from boardman.github.qa_activity_inference import infer_qa_tier_from_pr_activity


class _FakeResponse:
    def __init__(self, items: list[dict[str, Any]]):
        self.status_code = 200
        self._items = items

    def json(self) -> dict[str, Any]:
        return {"items": self._items}


class _FakeClient:
    """Returns PRs for author query page 1, empty for other pages/queries."""

    def __init__(self, author_items: list[dict[str, Any]]):
        self._author_items = author_items
        self.urls: list[str] = []

    async def get(self, url: str, headers: dict | None = None) -> _FakeResponse:
        self.urls.append(url)
        # GitHub search URLs percent-encode ":" in the query string.
        if "author" in url and "alice" in url and "page=1" in url:
            return _FakeResponse(self._author_items)
        return _FakeResponse([])


def _pr_item(repo: str = "org/heavy", num: int = 1, updated: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": num,
        "updated_at": updated or now,
    }


@pytest.mark.asyncio
async def test_infer_qa_tier_defaults_to_1_when_no_signal():
    client = _FakeClient([])
    repo_cache: dict[str, int] = {}
    tier, dbg = await infer_qa_tier_from_pr_activity(
        client,  # type: ignore[arg-type]
        "alice",
        "org",
        {},
        repo_cache,
        max_search_pages=1,
    )
    assert tier == 1
    assert dbg["pr_sample_count"] == 0


@pytest.mark.asyncio
async def test_infer_qa_tier_activity_tier3_with_cached_repos():
    """Enough distinct tier-3 repos + weighted score → tier 3."""
    items = [_pr_item("org/r1", 1), _pr_item("org/r2", 2), _pr_item("org/r3", 3)]
    client = _FakeClient(items)
    repo_cache = {"org/r1": 3, "org/r2": 3, "org/r3": 3}
    tier, dbg = await infer_qa_tier_from_pr_activity(
        client,  # type: ignore[arg-type]
        "alice",
        "org",
        {},
        repo_cache,
        max_search_pages=1,
        tier3_min_distinct_t3_repos=2,
        tier3_min_weighted_score=2.0,
        tier2_min_distinct_t2plus_repos=9,
        tier2_min_weighted_score=999.0,
    )
    assert tier == 3
    assert dbg["distinct_t3_repos"] >= 2
    assert dbg["weighted_score"] >= 2.0
