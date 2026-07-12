"""Meeting plan markdown validation (schema-like constraints) and fallback similarity checks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from pydantic import BaseModel, Field


def parse_h2_sections(markdown: str) -> dict[str, str]:
    """Split markdown on ``##`` headings. Keys are stripped heading text (original casing)."""
    lines = markdown.strip().splitlines()
    pairs: list[tuple[str, str]] = []
    current_title: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_body
        if current_title is not None:
            body = "\n".join(current_body).strip()
            pairs.append((current_title, body))
        current_title = None
        current_body = []

    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            flush()
            current_title = m.group(1).strip()
            current_body = []
        elif current_title is not None:
            current_body.append(line)
    flush()
    return {title: body for title, body in pairs}


def _norm_heading(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _find_section_body(sections: dict[str, str], accepted_titles: Iterable[str]) -> str | None:
    wanted = {_norm_heading(t) for t in accepted_titles}
    for title, body in sections.items():
        if _norm_heading(title) in wanted:
            return body
    return None


class SectionConstraint(BaseModel):
    """Zod-like per-section rules after splitting on H2 headings."""

    titles: tuple[str, ...] = Field(
        ...,
        description="Heading variants that satisfy this block (case-insensitive match).",
    )
    min_chars: int = Field(default=40, ge=1)
    require_any_of: tuple[str, ...] = Field(
        default=(),
        description="At least one substring must appear in the section body (case-insensitive).",
    )
    require_all_of: tuple[str, ...] = Field(
        default=(),
        description="Every substring must appear in the section body (case-insensitive).",
    )


DEFAULT_SECTION_CONSTRAINTS: tuple[SectionConstraint, ...] = (
    SectionConstraint(titles=("Purpose",), min_chars=25),
    SectionConstraint(
        titles=("Agenda Timeline", "Agenda timeline"),
        min_chars=30,
        require_any_of=(":", "0:", "1:", "minute", "min", "–", "-"),
    ),
    SectionConstraint(
        titles=("Group Round Table",),
        min_chars=80,
        require_all_of=("win", "blocker"),
        require_any_of=("work", "planning", "next"),
    ),
    SectionConstraint(
        titles=("Team Snapshot",),
        min_chars=40,
        require_any_of=("schedule", "stream", "team", "snapshot", "priority"),
    ),
    SectionConstraint(titles=("Decisions Needed",), min_chars=25),
    SectionConstraint(
        titles=("Risks and Blockers", "Risks & Blockers"),
        min_chars=25,
        require_any_of=("risk", "blocker", "dependency", "escalat", "constraint"),
    ),
    SectionConstraint(
        titles=("Action Items",),
        min_chars=35,
        require_any_of=("owner", "due", "[ ]", "[x]", "action"),
    ),
    SectionConstraint(
        titles=("Follow-up Checklist", "Follow up Checklist"),
        min_chars=25,
        require_any_of=("[ ]", "[x]", "follow", "checklist", "post"),
    ),
)


class PlanSchemaValidation(BaseModel):
    """Result of validating markdown against section rules."""

    ok: bool
    errors: tuple[str, ...] = Field(default_factory=tuple)


def validate_meeting_plan_markdown(
    markdown: str,
    *,
    section_constraints: tuple[SectionConstraint, ...] = DEFAULT_SECTION_CONSTRAINTS,
) -> PlanSchemaValidation:
    sections = parse_h2_sections(markdown)
    errors: list[str] = []

    if not sections:
        errors.append("No ## sections found; expected facilitator-style markdown with H2 headings.")
        return PlanSchemaValidation(ok=False, errors=tuple(errors))

    for spec in section_constraints:
        body = _find_section_body(sections, spec.titles)
        if body is None:
            errors.append(f"Missing required section heading matching one of: {', '.join(spec.titles)}")
            continue
        lowered = body.lower()
        if len(body) < spec.min_chars:
            errors.append(
                f"Section {spec.titles[0]!r} is too short "
                f"({len(body)} chars; need >= {spec.min_chars})."
            )
        for needle in spec.require_all_of:
            if needle.lower() not in lowered:
                errors.append(f"Section {spec.titles[0]!r} must mention {needle!r}.")
        if spec.require_any_of and not any(n.lower() in lowered for n in spec.require_any_of):
            opts = ", ".join(repr(s) for s in spec.require_any_of)
            errors.append(f"Section {spec.titles[0]!r} must include at least one of: {opts}.")

    return PlanSchemaValidation(ok=len(errors) == 0, errors=tuple(errors))


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "more",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _word_bag(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def similarity_to_fallback(candidate: str, fallback: str) -> float:
    """Score in [0, 1]; higher means closer to the deterministic fallback."""
    a = _collapse_ws(candidate)
    b = _collapse_ws(fallback)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    wa, wb = _word_bag(candidate), _word_bag(fallback)
    jaccard = 0.0 if not wa or not wb else len(wa & wb) / len(wa | wb)
    return 0.62 * seq + 0.38 * jaccard


def is_too_close_to_fallback(
    candidate: str,
    fallback: str,
    *,
    max_similarity: float = 0.86,
) -> bool:
    """True if ``candidate`` is likely a near-copy of the deterministic template."""
    return similarity_to_fallback(candidate, fallback) >= max_similarity
