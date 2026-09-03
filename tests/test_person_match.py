"""Typed names resolve to people without an extra model turn — and refuse when unsure."""

from __future__ import annotations

from dataclasses import dataclass

from boardman.assignment.person_match import (
    ambiguous_candidates,
    best_member_for_name,
)


@dataclass(frozen=True)
class M:
    id: str
    display: str
    github_login: str = ""


ROSTER = [
    M("481106", "Ali Ferris", "Blasted-ctrl"),
    M("476634", "Sergio Vargas", "sergiovargas111"),
    M("460725", "Charles Huang", "charleshuang"),
    M("470001", "Asheen Hameeda", "hameeda-a"),
    M("470002", "Andy Nguyen", "AndyN-star"),
    M("470003", "Joe Black", "joeblack"),
]


def _name(query: str, roster=ROSTER) -> str | None:
    hit = best_member_for_name(query, roster)
    return hit.member.display if hit else None


def test_first_name_resolves() -> None:
    assert _name("Ali") == "Ali Ferris"
    assert _name("sergio") == "Sergio Vargas"
    assert _name("hameeda") == "Asheen Hameeda"


def test_full_name_and_case_insensitive() -> None:
    assert _name("ali ferris") == "Ali Ferris"
    assert _name("SERGIO VARGAS") == "Sergio Vargas"


def test_github_login_resolves() -> None:
    assert _name("sergiovargas111") == "Sergio Vargas"
    assert _name("Blasted-ctrl") == "Ali Ferris"
    assert _name("AndyN-star") == "Andy Nguyen"


def test_login_variant_without_separator() -> None:
    assert _name("blastedctrl") == "Ali Ferris"


def test_initial_plus_surname() -> None:
    assert _name("a ferris") == "Ali Ferris"


def test_typo_still_resolves() -> None:
    assert _name("sergioo") == "Sergio Vargas"
    assert _name("Ali Ferriss") == "Ali Ferris"


def test_unknown_name_returns_nothing() -> None:
    assert _name("Zoltan Kodaly") is None
    assert _name("") is None


def test_short_fragment_does_not_match_a_random_substring() -> None:
    """'an' appears inside Hameeda/Huang; a fragment must not assign a person."""
    assert _name("an") is None


def test_ambiguity_refuses_rather_than_guessing() -> None:
    roster = [
        M("1", "Chris Palmer", "cpalmer"),
        M("2", "Chris Nolan", "cnolan"),
    ]
    assert best_member_for_name("chris", roster) is None
    names = ambiguous_candidates("chris", roster)
    assert "Chris Palmer" in names and "Chris Nolan" in names


def test_ambiguity_resolves_once_the_surname_is_given() -> None:
    roster = [
        M("1", "Chris Palmer", "cpalmer"),
        M("2", "Chris Nolan", "cnolan"),
    ]
    hit = best_member_for_name("chris nolan", roster)
    assert hit and hit.member.id == "2"


def test_same_person_listed_twice_is_not_ambiguous() -> None:
    """The roster and the yaml fallback both carry a member; that is one human."""
    dupes = [M("481106", "Ali Ferris", "Blasted-ctrl"), M("481106", "Ali Ferris", "Blasted-ctrl")]
    hit = best_member_for_name("ali", dupes)
    assert hit and hit.member.id == "481106"


def test_exclude_login_keeps_the_author_out() -> None:
    hit = best_member_for_name("ali", ROSTER, exclude_login="Blasted-ctrl")
    assert hit is None


def test_match_carries_a_reason() -> None:
    hit = best_member_for_name("sergio", ROSTER)
    assert hit and hit.reason and hit.score >= 0.62


# --- the tool-facing resolver (this is the layer an ImportError slipped through) ------

BOTS_SCHEMA = {
    "fields": [
        {"key": "person-5", "name": "Assignee", "type": "PERSON"},
        {"key": "person-6", "name": "QA Engineer Assigned", "type": "PERSON"},
        {"key": "status-8", "name": "Status", "type": "STATUS"},
    ]
}


def _patch_roster(monkeypatch) -> None:
    class Cfg:
        members = ROSTER
        fallback_members: list = []

    # plaky_tools binds the name at import time, so patch it where it is looked up.
    monkeypatch.setattr(
        "boardman.agent.tools.plaky_tools.load_team_assignments", lambda *a, **k: Cfg()
    )


def test_resolver_maps_names_onto_the_boards_person_columns(monkeypatch) -> None:
    from boardman.agent.tools.plaky_tools import resolve_people_to_field_values

    _patch_roster(monkeypatch)
    fv, notes = resolve_people_to_field_values(assignee="Ali", qa="sergio", normalized=BOTS_SCHEMA)
    assert fv == {"person-5": "481106", "person-6": "476634"}
    assert any("assignee ->" in n for n in notes)
    assert any("qa ->" in n for n in notes)


def test_resolver_leaves_an_unknown_name_unset_and_says_so(monkeypatch) -> None:
    from boardman.agent.tools.plaky_tools import resolve_people_to_field_values

    _patch_roster(monkeypatch)
    fv, notes = resolve_people_to_field_values(
        assignee="Zoltan Kodaly", qa="", normalized=BOTS_SCHEMA
    )
    assert fv == {}
    assert any("did not match" in n for n in notes)


def test_resolver_is_a_no_op_without_names(monkeypatch) -> None:
    from boardman.agent.tools.plaky_tools import resolve_people_to_field_values

    _patch_roster(monkeypatch)
    assert resolve_people_to_field_values(assignee="", qa="", normalized=BOTS_SCHEMA) == ({}, [])


# --- review blockers: wrong-person matches that reached the live roster --------------


def test_surname_match_with_a_different_given_name_is_refused() -> None:
    """identity_match returns a 420 sentinel meaning "surnames agree, given names do
    NOT". Treating it as evidence resolved 'Bob Huang' to Charles Huang on the real
    roster, and 'John San' to Sean San."""
    roster = [
        M("1", "Charles Huang", "charleshuang"),
        M("2", "Sean San", "seansan"),
        M("3", "Eric Chen", "ericchen"),
    ]
    for query in ("Bob Huang", "John San", "Sara Chen", "Eric San"):
        assert best_member_for_name(query, roster) is None, query


def test_duplicated_roster_still_refuses_an_ambiguous_first_name() -> None:
    """The live config lists everyone twice (GitHub roster + yaml fallback). The twin
    became the runner-up, so the ambiguity margin compared a person to themselves and
    'chris' silently resolved instead of asking."""
    chris_a = M("1", "Christian Krider", "ckrider")
    chris_b = M("2", "Charles Huang", "charleshuang")
    doubled = [chris_a, chris_b, chris_a, chris_b]
    assert best_member_for_name("chris", doubled) is None


def test_duplicates_do_not_break_a_clear_match() -> None:
    doubled = ROSTER + ROSTER
    hit = best_member_for_name("sergio", doubled)
    assert hit and hit.member.id == "476634"


def test_initials_match_when_the_query_is_abbreviated() -> None:
    """A typed abbreviated given name ('a ferris') still resolves to the full
    roster name -- handled by the graded surname/initials scorer, which requires
    the given names to plausibly agree rather than accepting a bare initial as a
    stand-in for any surname."""
    roster = [M("481106", "Ali Ferris", "Blasted-ctrl")]
    for query in ("a ferris", "Ali Ferris"):
        hit = best_member_for_name(query, roster)
        assert hit and hit.member.id == "481106", query


def test_exact_match_still_works_when_the_roster_itself_is_abbreviated() -> None:
    roster = [M("481106", "Ali F", "Blasted-ctrl")]
    hit = best_member_for_name("Ali F", roster)
    assert hit and hit.member.id == "481106"


def test_full_name_no_longer_guesses_against_an_abbreviated_roster_entry() -> None:
    """A real false-positive incident in the sibling deepiri-norozo matcher: 'Joe
    Black' spuriously matched an unrelated roster entry stored as 'Joe H' at high
    confidence, because a bare surname initial on the CANDIDATE side used to stand
    in for any surname starting with that letter. Typing someone's full name must
    not resolve to a different person merely because the roster abbreviates their
    surname to the same initial."""
    roster = [M("1", "Joe H", "joeh")]
    assert best_member_for_name("Joe Hauer", roster) is None
    assert best_member_for_name("Joe Black", roster) is None
