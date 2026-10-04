"""Name resolution for ClickUp members (shared by the agent tools)."""

from __future__ import annotations

from boardman.clickup import people

USERS = [
    {"id": "11", "name": "Ali Fahad", "email": "ali@x.io"},
    {"id": "12", "name": "Sergio Vargas", "email": "sergio@x.io"},
    {"id": "13", "name": "Ali Khan", "email": "alik@x.io"},
]


def test_match_person_by_name_and_email():
    assert people.match_person("sergio", USERS)[0]["id"] == "12"
    assert people.match_person("sergio@x.io", USERS)[0]["id"] == "12"


def test_ambiguous_and_unknown_are_problems_not_guesses():
    user, problem = people.match_person("ali", USERS)
    assert user is None and "ambiguous" in problem
    user, problem = people.match_person("zzz", USERS)
    assert user is None and "no workspace member" in problem
    assert people.match_person("", USERS) == (None, "")


def test_assignee_ids_are_integers_or_none():
    assert people.assignee_ids({"id": "12"}) == [12]
    assert people.assignee_ids({"id": "abc"}) is None
    assert people.assignee_ids(None) is None
    assert people.assignee_ids({}) is None  # no id key must not raise


def test_match_floor_comes_from_settings(monkeypatch):
    monkeypatch.setattr(people.settings, "clickup_person_match_min_score", 900)
    user, problem = people.match_person("sergio", USERS)
    assert user is None and "no workspace member" in problem


async def test_resolve_people_reports_problems_per_role():
    class Fake:
        async def list_workspace_users(self):
            return {"ok": True, "users": USERS}

    add, qa, problems = await people.resolve_people(Fake(), "sergio", "ali")
    assert add == [12] and qa == "" and list(problems) == ["qa"]
    assert await people.resolve_people(Fake(), "", "") == (None, "", {})
