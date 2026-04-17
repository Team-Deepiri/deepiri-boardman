"""
Repo tier classification — pure IDF scoring, zero hardcoded weights or categories.

Score(repo) = Σ idf(signal)  for each signal present in the repo
Tier        = percentile bucket of that score across the org

idf(signal) and percentile thresholds are loaded from repo_signals.json,
which is written by scripts/sync_qa_capabilities.py after scanning the org.

Nothing here encodes domain knowledge. Weights emerge from frequency.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Literal, Optional

_log = logging.getLogger(__name__)

Tier = Literal[1, 2, 3]


@dataclass
class TierScore:
    idf_score: float = 0.0
    structural_score: float = 0.0
    total: float = 0.0


def compute_structural_complexity_score(meta) -> float:
    """
    Compute structural complexity from signal COUNT statistics only.
    ZERO hardcoded names - only derived structural statistics.
    """
    counts = getattr(meta, "signal_counts", {})
    top_dirs = getattr(meta, "top_level_dirs", [])
    max_depth = getattr(meta, "max_depth", 0)

    total_files = sum(counts.values())
    total_dirs = sum(1 for k, v in counts.items() if k.startswith("dir:"))

    meaningful_top = len([d for d in top_dirs if not d.startswith(".")])

    score = 0.0
    score += meaningful_top * 8.0
    score += max_depth * 4.0
    score += min(total_files / 30.0, 15.0)
    score += min(total_dirs / 8.0, 10.0)

    dir_sizes = {}
    for k, v in counts.items():
        if k.startswith("dir:"):
            dir_sizes[k] = v
    if len(dir_sizes) > 3:
        avg = sum(dir_sizes.values()) / len(dir_sizes)
        variance = sum((v - avg) ** 2 for v in dir_sizes.values()) / len(dir_sizes)
        if variance > avg:
            score += 10.0

    return score


def compute_structural_complexity_score(meta) -> float:
    """
    Compute structural complexity purely from signal frequencies.
    ZERO hardcoded roles or keyword lists.
    """
    score = 0.0
    counts = getattr(meta, "signal_counts", {})
    raw_sigs = getattr(meta, "raw_signals", [])
    top_dirs = getattr(meta, "top_level_dirs", [])
    max_depth = getattr(meta, "max_depth", 0)

    meaningful_top = [d for d in top_dirs if not d.startswith(".")]
    score += len(meaningful_top) * 10.0

    score += max_depth * 5.0

    entry_points = counts.get("file:main.py", 0)
    entry_points += counts.get("file:server.py", 0)
    entry_points += counts.get("file:app.py", 0)
    entry_points += counts.get("file:index.js", 0)
    entry_points += counts.get("file:run.py", 0)
    if entry_points > 1:
        score += 25.0 * (entry_points - 1)

    docker_count = counts.get("file:docker-compose.yml", 0)
    docker_count += counts.get("file:docker-compose.yaml", 0)
    if docker_count > 1:
        score += 30.0 * (docker_count - 1)

    infra_dirs = sum(1 for s in raw_sigs if s.startswith("dir:terraform") or s.startswith("dir:helm") or s.startswith("dir:k8s") or s.startswith("dir:.github"))
    if infra_dirs > 0:
        score += infra_dirs * 12.0

    configs = sum(v for k, v in counts.items() if k.startswith("file:") and (k.endswith(".yml") or k.endswith(".yaml") or k.endswith(".toml") or k.endswith(".json") or k.endswith(".config.")))
    if configs > 5:
        score += 15.0

    total_files = sum(counts.values())
    if total_files > 100:
        score += 10.0
    if total_files > 500:
        score += 15.0

    return score


# ── Module-level IDF cache (reloads when file changes) ───────────────────────
_cache: dict = {}
_warned_missing = False


def _median_abs_deviation(scores: list[float]) -> float:
    """Compute MAD - robust outlier-resistant measure of spread."""
    if len(scores) < 2:
        return scores[0] if scores else 0
    sorted_scores = sorted(scores)
    median = sorted_scores[len(sorted_scores) // 2]
    deviations = [abs(s - median) for s in sorted_scores]
    deviations.sort()
    return deviations[len(deviations) // 2]


def _load() -> tuple[dict[str, float], dict[str, float]]:
    """Return (idf_weights, percentiles). Falls back to ({}, {}) if file missing."""
    global _cache, _warned_missing

    from boardman.settings import settings
    path = settings.repo_signals_json_path

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if not _warned_missing:
            _log.warning(
                "repo_signals.json not found at %r. "
                "Run scripts/sync_qa_capabilities.py to generate it. "
                "Defaulting all repos to tier 2 until then.",
                path,
            )
            _warned_missing = True
        return {}, {}

    if _cache.get("mtime") == mtime:
        return _cache["idf"], _cache["percentiles"]

    try:
        with open(path) as f:
            data = json.load(f)
        _cache = {
            "mtime": mtime,
            "idf": data.get("idf", {}),
            "percentiles": data.get("percentiles", {}),
        }
        _warned_missing = False
        _log.info(
            "Loaded IDF data: %d signals, p50=%.2f, p80=%.2f",
            len(_cache["idf"]),
            _cache["percentiles"].get("p50", 0),
            _cache["percentiles"].get("p80", 0),
        )
        return _cache["idf"], _cache["percentiles"]
    except Exception as exc:
        _log.warning("Failed to load repo_signals.json: %s — defaulting to tier 2", exc)
        return {}, {}


def classify_repo_tier(meta) -> tuple[Tier, TierScore]:
    """Pure IDF ranking - fully dynamic."""
    if not meta:
        return 3, TierScore()

    idf_data, percentiles = _load()
    if not idf_data:
        return 3, TierScore()

    idf_score = sum(idf_data.get(sig, 0.0) for sig in getattr(meta, "raw_signals", []))
    structural_score = compute_structural_complexity_score(meta)

    final_score = idf_score

    ts = TierScore(idf_score=idf_score, structural_score=structural_score, total=final_score)

    p50 = percentiles.get("p50", 50)
    p80 = percentiles.get("p80", 200)
    
    if final_score >= p80:
        return 3, ts
    if final_score >= p50:
        return 2, ts
    return 1, ts


def classify_repos_tier(metadata_map: dict) -> dict[str, Tier]:
    return {fn: classify_repo_tier(meta)[0] for fn, meta in metadata_map.items()}
