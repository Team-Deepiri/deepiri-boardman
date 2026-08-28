"""Render cognition verdicts and contradictions into a capped prompt block.

Pure function, no I/O. Follows the provenance-stamped style of render_project_state.
"""

from __future__ import annotations


def render_cognition_block(cognition: dict | None, *, max_chars: int = 1500) -> str:
    """Render cognition state as structured text for the LLM prompt."""
    if not cognition or not isinstance(cognition, dict):
        return ""

    state = cognition.get("cognition_state", "unavailable")
    verdicts = cognition.get("verdicts") or []
    contradictions = cognition.get("contradictions") or []

    if not verdicts and not contradictions:
        return ""

    lines: list[str] = ["\n## Cognition (intent vs reality)"]
    if state == "stale":
        lines.append("_cognition data is stale; verdicts may be outdated_")

    non_aligned = [v for v in verdicts if v.get("conclusion") != "ALIGNED"]
    aligned_count = len(verdicts) - len(non_aligned)

    if aligned_count:
        lines.append(f"- {aligned_count} behavior(s) ALIGNED")
    for v in non_aligned[:8]:
        key = v.get("behavior_key", "?")
        conclusion = v.get("conclusion", "?")
        explanation = v.get("explanation", "")
        confidence = v.get("confidence", "")
        conf_tag = f" [{confidence}]" if confidence else ""
        lines.append(f"- **{conclusion}**{conf_tag} `{key}`: {explanation}")
        for ev in (v.get("evidence") or [])[:2]:
            kind = ev.get("kind", "?")
            source_ref = ev.get("source_ref", "")
            value = ev.get("value", "")[:80]
            lines.append(f"  - [{kind}] {source_ref}: {value}")

    if contradictions:
        lines.append("")
        lines.append(f"### Open contradictions ({len(contradictions)})")
        for c in contradictions[:6]:
            entity = c.get("entity", "?")
            desc = c.get("description", "")[:100]
            severity = c.get("severity", "low")
            lines.append(f"- [{severity}] {entity}: {desc}")

    text = "\n".join(lines)
    return text[:max_chars]
