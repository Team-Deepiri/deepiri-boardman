"""GitHub ↔ Plaky identity matching — core cases and edge cases."""

from __future__ import annotations

from boardman.assignment.identity_match import (
    best_plaky_match_for_github,
    score_github_vs_plaky,
)
from boardman.settings import settings


def test_exact_email():
    gh = {"login": "alice", "name": "Alice", "email": "alice@deepiri.com"}
    pl = {"id": "p1", "name": "Alice X", "email": "alice@deepiri.com"}
    assert score_github_vs_plaky(gh, pl) >= 9000


def test_same_local_different_domain():
    gh = {"login": "bob", "email": "bob@github.com"}
    pl = {"id": "p2", "email": "bob@deepiri.com"}
    assert score_github_vs_plaky(gh, pl) >= 8500


def test_login_equals_plaky_local():
    gh = {"login": "jsmith", "name": ""}
    pl = {"id": "p3", "email": "jsmith@company.org"}
    assert score_github_vs_plaky(gh, pl) >= 8500


def test_similar_email_typo():
    gh = {"login": "x", "email": "joseph@deepiri.com"}
    pl = {"id": "p4", "email": "josef@deepiri.com"}
    assert score_github_vs_plaky(gh, pl) >= 640


def test_john_doe_vs_jonathan_doe():
    gh = {"login": "jdoe", "name": "John Doe"}
    pl = {"id": "p5", "name": "Jonathan Doe", "email": ""}
    assert score_github_vs_plaky(gh, pl) >= 640


def test_john_smith_vs_jane_smith_not_same_person():
    """Same initial + common surname must not score as a strong name match."""
    gh = {"login": "jsmith", "name": "John Smith"}
    pl = {"id": "p6", "name": "Jane Smith", "email": ""}
    assert score_github_vs_plaky(gh, pl) < 640


def test_best_match_ambiguous_two_identical_display_names():
    gh = {"login": "sam", "name": "Sam Smith", "email": "sam@x.com"}
    users = [
        {"id": "a", "name": "Sam Smith", "email": "other@y.com"},
        {"id": "b", "name": "Sam Smith", "email": "diff@z.com"},
    ]
    mid, reason, _ = best_plaky_match_for_github(gh, users, min_score=600, ambiguity_margin=50)
    assert mid is None
    assert reason == "ambiguous"


def test_best_match_three_way_cluster_ambiguous():
    gh = {"login": "u", "name": "Alex Row", "email": "a@x.com"}
    users = [
        {"id": "1", "name": "Alex Row", "email": "e1@x.com"},
        {"id": "2", "name": "Alex Row", "email": "e2@x.com"},
        {"id": "3", "name": "Alex Row", "email": "e3@x.com"},
    ]
    mid, reason, sc = best_plaky_match_for_github(gh, users, min_score=5000, ambiguity_margin=80)
    assert mid is None
    assert reason == "ambiguous"
    assert sc > 0


def test_best_match_clear_winner():
    gh = {"login": "ada", "email": "ada@co.com"}
    users = [
        {"id": "weak", "name": "Bob", "email": "bob@co.com"},
        {"id": "win", "name": "Ada", "email": "ada@co.com"},
    ]
    mid, reason, sc = best_plaky_match_for_github(gh, users, min_score=640)
    assert reason == "matched"
    assert mid == "win"
    assert sc >= 640


def test_last_comma_first_order():
    gh = {"login": "cd", "name": "Doe, Caroline"}
    pl = {"id": "p7", "name": "Caroline Doe", "email": ""}
    assert score_github_vs_plaky(gh, pl) >= 8000


def test_unicode_name_folded():
    gh = {"login": "jose", "name": "José García"}
    pl = {"id": "p8", "name": "Jose Garcia", "email": ""}
    assert score_github_vs_plaky(gh, pl) >= 8000


def test_plus_address_local_equivalence():
    gh = {"login": "a", "email": "user+github@deepiri.com"}
    pl = {"id": "p9", "email": "user@deepiri.com"}
    assert score_github_vs_plaky(gh, pl) >= 8500


def test_gmail_dot_equivalence():
    gh = {"login": "a", "email": "first.last@gmail.com"}
    pl = {"id": "p10", "email": "firstlast@gmail.com"}
    assert score_github_vs_plaky(gh, pl) >= 8500


def test_plaky_multiple_emails_second_matches():
    gh = {"login": "mix", "email": "work@corp.com"}
    pl = {
        "id": "p11",
        "name": "Mixer",
        "email": "personal@gmail.com",
        "emails": ["alias@other.org", "work@corp.com"],
    }
    assert score_github_vs_plaky(gh, pl) >= 9000


def test_angle_brackets_in_email_field():
    gh = {"login": "x", "email": "Human <human@co.com>"}
    pl = {"id": "p12", "email": "human@co.com"}
    assert score_github_vs_plaky(gh, pl) >= 9000


def test_login_john_smith_vs_dotted_email():
    gh = {"login": "john.smith", "name": ""}
    pl = {"id": "p13", "email": "john.smith@enterprise.io"}
    assert score_github_vs_plaky(gh, pl) >= 8500


def test_formal_vs_informal_first_name_stays_weak_without_llm():
    """No hardcoded Bob→Robert map; structural score stays below threshold."""
    gh = {"login": "rb", "name": "Bob Builder"}
    pl = {"id": "p14", "name": "Robert Builder", "email": ""}
    assert score_github_vs_plaky(gh, pl) < 640


def test_llm_can_lift_gray_zone_nickname_pair(monkeypatch):
    gh = {"login": "rb", "name": "Bob Builder"}
    pl = {"id": "p14", "name": "Robert Builder", "email": ""}
    monkeypatch.setattr(settings, "assignment_identity_llm_enabled", True)
    monkeypatch.setattr(
        "boardman.assignment.llm_identity_match.llm_same_person_confidence",
        lambda _gh, _pl: 0.95,
    )
    assert score_github_vs_plaky(gh, pl) >= 8000


def test_llm_reject_capped_when_model_says_different(monkeypatch):
    gh = {"login": "rb", "name": "Bob Builder"}
    pl = {"id": "p14", "name": "Robert Builder", "email": ""}
    monkeypatch.setattr(settings, "assignment_identity_llm_enabled", True)
    monkeypatch.setattr(
        "boardman.assignment.llm_identity_match.llm_same_person_confidence",
        lambda _gh, _pl: 0.05,
    )
    assert score_github_vs_plaky(gh, pl) < 500


def test_unrelated_users_low_score():
    gh = {"login": "zebra", "name": "Zebra Alpha", "email": "z@z.z"}
    pl = {"id": "p15", "name": "Quasar Beta", "email": "q@q.q"}
    assert score_github_vs_plaky(gh, pl) < 640


def test_empty_github_profile_no_accidental_login_token_hit():
    gh = {"login": "ghost", "name": "", "email": ""}
    pl = {"id": "p16", "name": "Ghastly Other", "email": ""}
    assert score_github_vs_plaky(gh, pl) < 640


def test_login_initial_lastname_with_github_display_name():
    gh = {"login": "jsmith", "name": "John Smith"}
    pl = {"id": "p17", "name": "John Smith", "email": "x@y.z"}
    assert score_github_vs_plaky(gh, pl) >= 640


def test_identical_scores_return_ambiguous():
    """Stable sort keeps first id, but equal scores must not auto-assign."""
    users = [
        {"id": "first-wins-sort", "name": "Pat Lee", "email": "a@a.com"},
        {"id": "second", "name": "Pat Lee", "email": "b@b.com"},
    ]
    gh = {"login": "pat", "name": "Pat Lee", "email": ""}
    assert score_github_vs_plaky(gh, users[0]) == score_github_vs_plaky(gh, users[1])
    mid, reason, _ = best_plaky_match_for_github(gh, users, min_score=8000, ambiguity_margin=5000)
    assert mid is None
    assert reason == "ambiguous"
