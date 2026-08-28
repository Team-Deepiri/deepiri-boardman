"""Intent-vs-reality engine: deterministic file/function existence checks.

Compare what the docs/specs intend against what the code actually provides, producing
a per-behavior verdict without an LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from boardman.cognition.evidence import BehaviorSpec, Evidence, evidence_to_dict


@dataclass(frozen=True)
class BehaviorVerdict:
    behavior_key: str
    conclusion: Literal["ALIGNED", "PARTIAL", "BROKEN", "UNKNOWN"]
    confidence: Literal["high", "low"]
    evidence: tuple[Evidence, ...]
    explanation: str


def _repo_root() -> Path | None:
    """Best-effort repo root for local file checks."""
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "boardman").is_dir():
        return candidate
    return None


def _check_expected_present(entry: str, root: Path) -> bool | None:
    """Check one expected_present entry. Returns True/False/None (unresolvable)."""
    if ":" in entry:
        file_part, func_name = entry.rsplit(":", 1)
    else:
        file_part, func_name = entry, ""

    target = root / file_part
    if not target.is_file():
        return False

    if not func_name:
        return True

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return func_name in content


def compare_intent_to_reality(spec: BehaviorSpec) -> BehaviorVerdict:
    """Deterministic check: do the expected files/functions still exist?"""
    from boardman.observability.counters import bump

    root = _repo_root()
    if root is None or not spec.expected_present:
        bump("cognition.verdicts.unknown")
        return BehaviorVerdict(
            behavior_key=spec.behavior_key,
            conclusion="UNKNOWN",
            confidence="low",
            evidence=spec.evidence,
            explanation="source unavailable for verification",
        )

    present = 0
    absent = 0
    for entry in spec.expected_present:
        result = _check_expected_present(entry, root)
        if result is None:
            bump("cognition.verdicts.unknown")
            return BehaviorVerdict(
                behavior_key=spec.behavior_key,
                conclusion="UNKNOWN",
                confidence="low",
                evidence=spec.evidence,
                explanation=f"could not read {entry}",
            )
        if result:
            present += 1
        else:
            absent += 1

    if absent == 0:
        bump("cognition.verdicts.aligned")
        return BehaviorVerdict(
            behavior_key=spec.behavior_key,
            conclusion="ALIGNED",
            confidence="high",
            evidence=spec.evidence,
            explanation=f"all {present} expected items confirmed present",
        )
    if present == 0:
        bump("cognition.verdicts.broken")
        return BehaviorVerdict(
            behavior_key=spec.behavior_key,
            conclusion="BROKEN",
            confidence="high",
            evidence=spec.evidence,
            explanation=f"all {absent} expected items confirmed absent",
        )
    bump("cognition.verdicts.partial")
    return BehaviorVerdict(
        behavior_key=spec.behavior_key,
        conclusion="PARTIAL",
        confidence="high",
        evidence=spec.evidence,
        explanation=f"{present} present, {absent} absent",
    )


def verdict_to_dict(v: BehaviorVerdict) -> dict[str, Any]:
    return {
        "behavior_key": v.behavior_key,
        "conclusion": v.conclusion,
        "confidence": v.confidence,
        "evidence": [evidence_to_dict(e) for e in v.evidence],
        "explanation": v.explanation,
    }


async def compute_cognition_verdicts(repo: str, session: Any) -> None:
    """Iterate all registered behaviors, compute verdicts, persist to context_json."""
    from boardman.agent.repo_context import save_cognition_state
    from boardman.cognition.behaviors import BEHAVIORS

    verdicts = []
    for spec in BEHAVIORS:
        v = compare_intent_to_reality(spec)
        verdicts.append(verdict_to_dict(v))

    from datetime import datetime

    cognition: dict[str, Any] = {
        "cognition_state": "fresh",
        "verdicts": verdicts,
        "computed_at": datetime.utcnow().isoformat(),
    }
    await save_cognition_state(session, repo, cognition)
