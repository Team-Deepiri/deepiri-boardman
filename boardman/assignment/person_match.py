"""Resolve a free-text person name to a roster member.

People do not type Plaky user ids. They say "assign it to Ali", "sergio should QA
this", "give it to andy". Before this module the assistant's only route was to call
`plaky_list_workspace_users` and eyeball the result, which cost an extra model turn per
task and still guessed. This resolves the name locally, deterministically, in
microseconds, against the same roster the QA picker uses.

Scoring reuses `identity_match` primitives (accent folding, surname overlap, initials,
login token splitting) but deliberately NOT `score_github_vs_plaky`: that function
clamps name-only evidence below its own acceptance floor unless an email or login
anchor corroborates it, which is right for silent background dedupe and wrong here,
where the typed name IS the whole signal.

Ambiguity is a refusal, never a coin flip. "chris" against two Chrises returns no match
with both names in the reason, so the assistant can ask instead of assigning the wrong
person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boardman.assignment.identity_match import (
    _canonical_full_name,
    _login_token_variants,
    _name_match_score,
    _name_tokens,
    _norm_ws_casefold,
    _similar,
)

# A typed name must clear this to count at all.
MIN_SCORE = 0.62
# The runner-up must be this far behind, or the query is ambiguous.
AMBIGUITY_MARGIN = 0.08
# Lowest tier of _name_match_score worth trusting: given names plausibly the same.
# Anything below is surname-only or weaker, which identity_match itself rejects.
_GRADED_ACCEPT = 4800


@dataclass(frozen=True)
class PersonMatch:
    member: Any
    score: float
    reason: str


def _display(member: Any) -> str:
    return str(getattr(member, "display", "") or "").strip()


def _login(member: Any) -> str:
    return str(getattr(member, "github_login", "") or "").strip()


def _score_one(query: str, member: Any) -> tuple[float, str]:
    """Best evidence that `query` names `member`, 0.0-1.0 with a human reason."""
    q = _norm_ws_casefold(query)
    if not q:
        return 0.0, ""

    display, login = _display(member), _login(member)
    multi_token = len(_name_tokens(query)) > 1
    best, why = 0.0, ""

    for field, value in (("name", display), ("login", login)):
        if not value:
            continue
        v = _norm_ws_casefold(value)
        if q == v:
            return 1.0, f"exact {field} match"
        # "ali" inside "Ali Ferris": a full token, not a random substring — "an" must
        # not match "Nathan".
        tokens = _name_tokens(value) or [v]
        if q in tokens:
            cand = 0.93 if field == "name" else 0.9
            if cand > best:
                best, why = cand, f"first-name match on {field} {value!r}"
        # "ali ferris" against "Ali Ferris"
        q_tokens = _name_tokens(query)
        # Exact token match only -- no longer accepts a bare initial ("h") as
        # standing in for any full token starting with that letter ("hauer",
        # "harrison", "henderson", ...). That shortcut was a real false-positive
        # incident in deepiri-norozo's sibling matcher: "Joe Black" vs a roster
        # entry shaped like "Joe H<something>" scored 0.9 -- confident-looking,
        # but a bare initial is compatible with dozens of unrelated surnames in
        # any real-size roster, and the ambiguity check only catches the
        # collision when a second same-shaped candidate happens to also be
        # present, not when it's the only person with that first name + initial.
        # An abbreviated "Firstname L." query still gets a chance via the
        # graded surname/initials scorer below, which is far more conservative.
        if len(q_tokens) > 1 and all(t in tokens for t in q_tokens):
            if best < 0.9:
                best, why = 0.9, f"all parts of {query!r} match {value!r}"
        # Raw character similarity is only trustworthy for a ONE-WORD query (a typo).
        # For "First Last" it is driven by the shared surname: "John San" scored 0.63
        # against "Sean San" and "Sara Chen" 0.67 against "Eric Chen". A full name must
        # agree on the given name, which the exact / all-parts / graded paths check.
        if not multi_token:
            ratio = _similar(_canonical_full_name(query) or q, _canonical_full_name(value) or v)
            if ratio > best:
                best, why = ratio, f"{int(ratio * 100)}% similar to {field} {value!r}"
        # A one-word query is compared to each name part too: "sergioo" against the
        # whole "sergio vargas" scores badly, against the token "sergio" it is a
        # near-miss typo. Guarded to >=4 chars so short fragments cannot win.
        if len(q) >= 4 and " " not in q:
            for token in tokens:
                if len(token) < 4:
                    continue
                tok_ratio = _similar(q, token) * 0.97  # a part is weaker than the whole
                if tok_ratio > best:
                    best, why = tok_ratio, f"close to {token!r} in {field} {value!r}"

    # "blastedctrl" vs "Blasted-ctrl": logins split on separators.
    if login:
        variants = _login_token_variants(login)
        if q in variants or q.replace(" ", "") in {x.replace(" ", "") for x in variants}:
            if best < 0.9:
                best, why = 0.9, f"login variant of {login!r}"

    # The graded surname/initials scorer. It returns 420 with the "last name only" flag
    # set to mean "the surnames match but the GIVEN names disagree" — identity_match
    # rejects that tier on purpose, and so must we: without this guard "Bob Huang"
    # resolved to Charles Huang and "John San" to Sean San, both at 0.677.
    if display:
        graded, last_name_only, _weak = _name_match_score(query, display)
        if not last_name_only and graded >= _GRADED_ACCEPT:
            # 4800 (given names plausibly the same) -> 0.80, 7400 (exact) -> 1.00.
            as_ratio = min(1.0, 0.80 + (graded - _GRADED_ACCEPT) / 13000.0)
            if as_ratio > best:
                best, why = as_ratio, f"name evidence for {display!r}"

    return best, why


def _unique_members(members: list[Any]) -> list[Any]:
    """One entry per human.

    The live config carries every teammate twice (`cfg.members` from the GitHub roster
    plus `cfg.fallback_members` from the yaml). Scoring both made every runner-up the
    top match's own twin, so the ambiguity check compared a person to themselves and
    never fired: "chris" silently picked Christian Krider over Charles Huang.
    """
    out: list[Any] = []
    seen: set[str] = set()
    for m in members:
        key = str(getattr(m, "id", "") or "").strip() or (f"{_display(m)}|{_login(m)}".casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def best_member_for_name(
    query: str,
    members: list[Any],
    *,
    min_score: float = MIN_SCORE,
    exclude_login: str = "",
) -> PersonMatch | None:
    """Highest-confidence member for a typed name, or None when unsure.

    None means "do not guess": either nothing cleared `min_score`, or two people are
    too close to separate. The caller should surface that rather than assign someone.
    """
    q = (query or "").strip()
    if not q or not members:
        return None
    skip = (exclude_login or "").strip().casefold()

    scored: list[tuple[float, str, Any]] = []
    for m in _unique_members(members):
        if skip and _login(m).casefold() == skip:
            continue
        s, why = _score_one(q, m)
        if s > 0:
            scored.append((s, why, m))
    if not scored:
        return None

    scored.sort(key=lambda row: row[0], reverse=True)
    top_score, top_why, top_member = scored[0]
    if top_score < min_score:
        return None
    if len(scored) > 1:
        runner_score, _why, runner = scored[1]
        # Two different people within the margin: ambiguous. Same person listed twice
        # (roster + fallback) is not.
        same_person = str(getattr(runner, "id", "")) == str(getattr(top_member, "id", ""))
        if not same_person and (top_score - runner_score) < AMBIGUITY_MARGIN:
            return None
    return PersonMatch(member=top_member, score=round(top_score, 3), reason=top_why)


def ambiguous_candidates(query: str, members: list[Any], *, limit: int = 4) -> list[str]:
    """Display names that plausibly match, for an honest 'which one did you mean?'."""
    q = (query or "").strip()
    if not q:
        return []
    rows: list[tuple[float, str]] = []
    seen: set[str] = set()
    for m in _unique_members(members):
        s, _why = _score_one(q, m)
        name = _display(m) or _login(m)
        if s >= 0.5 and name and name not in seen:
            seen.add(name)
            rows.append((s, name))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [n for _s, n in rows[:limit]]
