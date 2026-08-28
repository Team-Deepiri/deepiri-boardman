"""Typed evidence model for the cognition engine.

Evidence carries its source provenance so Boardman can separate what it knows for a fact
from what it infers, from what the docs intend, from what actually happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: Literal["fact", "inference", "intent", "observed"]
    subject: str
    value: str
    source_type: Literal["github", "plaky", "code", "test", "doc", "config", "runtime"]
    source_ref: str
    computed_at: str


@dataclass(frozen=True)
class BehaviorSpec:
    behavior_key: str
    description: str
    expected_present: tuple[str, ...]
    evidence: tuple[Evidence, ...]


def evidence_to_dict(e: Evidence) -> dict[str, Any]:
    return {
        "kind": e.kind,
        "subject": e.subject,
        "value": e.value,
        "source_type": e.source_type,
        "source_ref": e.source_ref,
        "computed_at": e.computed_at,
    }


def evidence_from_dict(d: dict[str, Any]) -> Evidence:
    return Evidence(
        kind=d["kind"],
        subject=d["subject"],
        value=d["value"],
        source_type=d["source_type"],
        source_ref=d["source_ref"],
        computed_at=d["computed_at"],
    )
