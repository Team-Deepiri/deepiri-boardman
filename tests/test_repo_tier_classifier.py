"""Repo tier from repo_signals.json + tier_classifier (no GitHub)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardman.assignment import tier_classifier as tc
from boardman.github.repo_metadata import RepoMetadata
from boardman.settings import settings


@pytest.fixture
def tier_signals_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal IDF + percentiles; scores align with classify_repo_tier (idf + 0.2 * structural)."""
    p = tmp_path / "repo_signals.json"
    monkeypatch.setattr(settings, "repo_signals_json_path", str(p))
    tc._cache.clear()
    tc._warned_missing = False
    # idf weights for signals we'll use in meta
    data = {
        "idf": {"file:foo.py": 1.0, "dir:src": 3.0, "lang:python": 0.5},
        "percentiles": {"p50": 2.0, "p80": 5.0},
        "repo_scores": {},
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _meta(signals: list[str], *, top_level_dirs: list[str] | None = None, max_depth: int = 0) -> RepoMetadata:
    counts: dict[str, int] = {}
    for s in signals:
        counts[s] = counts.get(s, 0) + 1
    return RepoMetadata(
        full_name="o/r",
        raw_signals=signals,
        signal_counts=counts,
        top_level_dirs=list(top_level_dirs or []),
        max_depth=max_depth,
    )


def test_classify_low_idf_is_tier1(tier_signals_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tc, "_warned_missing", False)
    tc._cache.clear()
    m = _meta(["file:foo.py", "lang:python"])
    tier, scores = tc.classify_repo_tier(m)
    # idf 1.5 + small structural < p50 (2.0)
    assert tier == 1
    assert scores.total < 2.0


def test_classify_mid_is_tier2(tier_signals_path: Path, monkeypatch: pytest.MonkeyPatch):
    tc._cache.clear()
    m = _meta(["file:foo.py", "dir:src", "lang:python"])
    tier, scores = tc.classify_repo_tier(m)
    # idf 1+3+0.5 + structural*0.2 — should land between p50 and p80
    assert tier == 2


def test_classify_high_is_tier3(tier_signals_path: Path, monkeypatch: pytest.MonkeyPatch):
    tc._cache.clear()
    # Push idf sum high
    p = Path(settings.repo_signals_json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["idf"]["dir:huge"] = 10.0
    data["percentiles"] = {"p50": 1.0, "p80": 3.0}
    p.write_text(json.dumps(data), encoding="utf-8")
    tc._cache.clear()
    m = _meta(["file:foo.py", "dir:src", "dir:huge", "lang:python"])
    tier, scores = tc.classify_repo_tier(m)
    assert tier == 3
    assert scores.total >= 3.0
