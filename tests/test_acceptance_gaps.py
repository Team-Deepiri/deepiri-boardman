"""Acceptance gaps from the closing meeting notes.

Three rules that were not enforced anywhere:

* a QA-only or IT account must never land in the DEVELOPER column, and an LLM asked
  nicely must not be able to put one there;
* only the five agreed Type values may be written, whatever GitHub labels say;
* a label or milestone event knows nothing about progress, so it must not drag a task
  that reached In QA back to Assigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from boardman.assignment.developer_eligibility import developer_eligibility, filter_developer
from boardman.github.pr_signals import infer_task_type_from_pr
from boardman.plaky.task_tag_vocab import canonical_task_type, type_field_patch_candidates
from boardman.services.sync_state import issue_status_intent, resolve_issue_state

ALLOWED_TYPES = {"Feature", "Bug", "Refactor", "Research", "Story"}


@dataclass
class M:
    id: str
    display: str = ""
    github_login: str = ""
    roles: list[str] = field(default_factory=list)


@dataclass
class Cfg:
    members: list[Any] = field(default_factory=list)
    fallback_members: list[Any] = field(default_factory=list)
    developer_excluded: list[str] = field(default_factory=list)


# --- developer eligibility -----------------------------------------------------------


def test_qa_only_member_is_not_developer_eligible() -> None:
    qa = M("1", "Quinn QA", "quinnqa", roles=["qa"])
    verdict = developer_eligibility(qa, Cfg(members=[qa]))
    assert verdict.ok is False
    assert "qa-only" in verdict.reason.lower()


def test_it_and_support_accounts_are_not_developer_eligible() -> None:
    for role in ("it", "support", "helpdesk", "tester"):
        person = M("2", "Sam Support", "samsupport", roles=[role])
        assert developer_eligibility(person, Cfg(members=[person])).ok is False, role


def test_engineer_is_eligible_even_when_also_qa() -> None:
    """The live roster gives everyone [engineer, qa]; that must stay assignable."""
    dev = M("3", "Ali Ferris", "Blasted-ctrl", roles=["engineer", "qa"])
    assert developer_eligibility(dev, Cfg(members=[dev])).ok is True


def test_explicit_developer_exclusion_by_name_or_login() -> None:
    dev = M("4", "Joe Black", "joeblack", roles=["engineer"])
    cfg = Cfg(members=[dev], developer_excluded=["Joe Black"])
    assert developer_eligibility(dev, cfg).ok is False
    cfg2 = Cfg(members=[dev], developer_excluded=["joeblack"])
    assert developer_eligibility(dev, cfg2).ok is False


def test_filter_developer_blocks_a_qa_only_id() -> None:
    qa = M("77", "Quinn QA", "quinnqa", roles=["qa"])
    kept, reason = filter_developer("77", Cfg(members=[qa]))
    assert kept == ""
    assert "not a developer" in reason


def test_filter_developer_passes_an_engineer_through() -> None:
    dev = M("88", "Dev Person", "devperson", roles=["engineer"])
    kept, reason = filter_developer("88", Cfg(members=[dev]))
    assert kept == "88" and reason == ""


def test_someone_off_the_roster_is_not_refused() -> None:
    """The roster is the QA team, not the payroll; refusing every unknown id would
    block assigning contractors who legitimately own work."""
    kept, reason = filter_developer("999", Cfg(members=[]))
    assert kept == "999" and reason == ""


def test_empty_id_is_a_no_op() -> None:
    assert filter_developer("", Cfg()) == ("", "")


def test_the_live_roster_still_yields_assignable_developers() -> None:
    """A rule that made everyone ineligible would be worse than no rule."""
    from boardman.assignment.config import load_team_assignments

    cfg = load_team_assignments()
    people = list(cfg.members) + list(getattr(cfg, "fallback_members", []) or [])
    eligible = [m for m in people if developer_eligibility(m, cfg).ok]
    assert eligible, "no member of the live roster can be assigned as a developer"


# --- GitHub labels -> Plaky Type ------------------------------------------------------

LIVE_LABELS = [
    "AI",
    "bug",
    "Chore",
    "dependencies",
    "DevOps",
    "documentation",
    "duplicate",
    "enhancement",
    "Feature",
    "good first issue",
    "help wanted",
    "JOE REVIEW REQUIRED",
    "NEEDS HELP",
    "python",
    "QA/Maintenance",
    "question",
    "Refactor",
    "wontfix",
]

BOARD_TYPE_OPTIONS = {
    "fields": [
        {
            "key": "status-7",
            "name": "Type",
            "type": "STATUS",
            "options": [
                {"id": "0", "name": "Story"},
                {"id": "9", "name": "Task"},
                {"id": "10", "name": "Bug"},
                {"id": "12", "name": "Research"},
                {"id": "17", "name": "Feature"},
                {"id": "18", "name": "Refactor"},
            ],
        }
    ]
}
OPTION_NAMES = {
    "0": "Story",
    "9": "Task",
    "10": "Bug",
    "12": "Research",
    "17": "Feature",
    "18": "Refactor",
}


def _type_written(label: str) -> str:
    from boardman.plaky.board_schema import select_field_patch_pair_from_schema

    canon = canonical_task_type(infer_task_type_from_pr(None, [label]) or "Feature")
    pair = select_field_patch_pair_from_schema(
        BOARD_TYPE_OPTIONS,
        column_name_substrings=("type", "issue type", "category", "kind"),
        value_label_candidates=type_field_patch_candidates(canon),
        exclude_name_substrings=("subtype",),
    )
    return OPTION_NAMES.get(str(pair[1]), str(pair[1])) if pair else "NONE"


@pytest.mark.parametrize("label", LIVE_LABELS)
def test_every_live_label_writes_one_of_the_five_types(label: str) -> None:
    assert _type_written(label) in ALLOWED_TYPES, f"{label} wrote a Type outside the five"


def test_the_obvious_labels_map_the_obvious_way() -> None:
    assert _type_written("bug") == "Bug"
    assert _type_written("Feature") == "Feature"
    assert _type_written("Refactor") == "Refactor"


def test_good_first_issue_is_not_a_bug() -> None:
    """It is an onboarding label. Loose containment of the token 'issue' typed it Bug."""
    assert infer_task_type_from_pr(None, ["good first issue"]) == ""
    assert _type_written("good first issue") != "Bug"


def test_an_exact_issue_label_still_resolves() -> None:
    assert infer_task_type_from_pr(None, ["issue"]) != ""
    assert infer_task_type_from_pr(None, ["type: issue"]) != ""


def test_the_relevant_label_wins_among_unrelated_ones() -> None:
    assert _type_written("bug") == "Bug"
    canon = canonical_task_type(
        infer_task_type_from_pr(None, ["python", "help wanted", "bug", "AI"]) or "Feature"
    )
    assert canon.casefold().startswith("bug")


# --- status: only ownership events may move it ----------------------------------------


def _issue(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 5,
        "title": "T",
        "body": "",
        "html_url": "u",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    base.update(over)
    return base


def test_intent_requires_a_resolved_person_before_claiming_assigned() -> None:
    """A GitHub assignee nobody on the board matches would write Assigned onto a task
    with an empty Assignee column."""
    st = resolve_issue_state(
        _issue(assignees=[{"login": "ghost"}]), repo_full_name="o/r", repo_name="r"
    )
    assert issue_status_intent(st, engineer_plaky_id="") == "workflow_needs_assigned"
    assert issue_status_intent(st, engineer_plaky_id="481106") == "workflow_assigned"
    # No opinion supplied: fall back to the GitHub login, the long-standing behaviour.
    assert issue_status_intent(st) == "workflow_assigned"


def test_closed_always_completes_regardless_of_owner() -> None:
    st = resolve_issue_state(_issue(state="closed"), repo_full_name="o/r", repo_name="r")
    assert issue_status_intent(st, engineer_plaky_id="") == "workflow_completed"


# --- board reads name people instead of printing plaky ids ----------------------------


def test_board_read_resolves_person_ids_to_names() -> None:
    """ "Assignee 481106" is a lookup table, not an answer."""
    from boardman.agent.tools.plaky_tools import _slim_task

    names = {"481106": "Ali F", "460725": "Charles Huang"}
    row = {
        "id": "1",
        "title": "T",
        "fields": [
            {"type": "PERSON", "title": "Assignee", "value": {"assignedUsers": ["481106"]}},
            {"type": "PERSON", "title": "QA", "value": {"assignedUsers": [{"id": "460725"}]}},
        ],
    }
    people = _slim_task(row, names)["assignees"]
    assert people[0]["users"] == ["Ali F"]
    assert people[1]["users"] == ["Charles Huang"]


def test_an_unknown_person_id_is_kept_not_invented() -> None:
    from boardman.agent.tools.plaky_tools import _slim_task

    people = _slim_task(
        {
            "id": "2",
            "title": "T",
            "fields": [
                {"type": "PERSON", "title": "Assignee", "value": {"assignedUsers": ["999999"]}}
            ],
        },
        {"481106": "Ali F"},
    )["assignees"]
    assert people[0]["users"] == ["999999"]


def test_the_live_roster_can_name_the_person_ids_on_the_board() -> None:
    from boardman.agent.tools.plaky_tools import _person_names

    names = _person_names()
    assert names, "roster produced no id -> name mapping"
    assert names.get("481106"), "the account that owns most board work is unnamed"


# --- a PR must not silently downgrade a priority a human set on the issue --------------


def test_a_pr_without_a_priority_label_has_no_priority_opinion() -> None:
    from boardman.services.sync_state import resolve_pr_state

    st = resolve_pr_state(
        {
            "number": 9,
            "title": "Fix the dropped checkout retry",
            "body": "Fixes #5",
            "html_url": "u",
            "user": {"login": "someone"},
            "head": {"ref": "fix/checkout"},
            "labels": [],
        },
        repo_full_name="o/r",
        repo_name="r",
    )
    assert st.priority_explicit is False


def test_a_priority_label_on_the_pr_is_an_opinion() -> None:
    from boardman.services.sync_state import resolve_pr_state

    st = resolve_pr_state(
        {
            "number": 9,
            "title": "Fix it",
            "body": "",
            "html_url": "u",
            "user": {"login": "someone"},
            "head": {"ref": "fix/x"},
            "labels": [{"name": "priority: urgent"}],
        },
        repo_full_name="o/r",
        repo_name="r",
    )
    assert st.priority_explicit is True
    assert st.priority == "Very Important"


def test_pr_handler_sends_no_priority_when_the_pr_has_no_label() -> None:
    """The guard has to be in the write path, not only in the state object."""
    import inspect

    from boardman.services import pr_handler

    src = inspect.getsource(pr_handler)
    bare = [
        line.strip()
        for line in src.splitlines()
        if "priority=" in line
        and "priority_explicit" not in line
        and "priority=priority" not in line
    ]
    assert not bare, f"unguarded PR priority write: {bare}"


# --- the create receipt has to name Type/Status however the model wrote them -----------

RECEIPT_INDEX = {
    "status-7": ("Type", {"10": "Bug", "17": "Feature"}),
    "status-8": ("Status", {"0": "NEEDS ASSIGNED", "8": "Assigned"}),
    "status-9": ("Priority", {"1": "High"}),
}


@pytest.mark.parametrize(
    ("field_values", "want"),
    [
        ({"status-7": "10"}, "Bug"),  # id, straight from the board schema
        ({"status-7": "Bug"}, "Bug"),  # the option name, which is what the model sends
        ({"status-7": "bug"}, "Bug"),  # case does not matter
        ({"status-7": {"id": "17"}}, "Feature"),  # wrapped value
        ({"status-7": ["17"]}, "Feature"),  # list value
    ],
)
def test_receipt_reads_the_type_the_model_wrote(field_values, want) -> None:
    from boardman.agent.tools.plaky_tools import _labelled_option

    assert _labelled_option(RECEIPT_INDEX, field_values, "type") == want


def test_receipt_says_nothing_rather_than_guessing() -> None:
    from boardman.agent.tools.plaky_tools import _labelled_option

    assert _labelled_option(RECEIPT_INDEX, {"status-7": "Epic"}, "type") == ""
    assert _labelled_option(RECEIPT_INDEX, None, "type") == ""
    assert _labelled_option({}, {"status-7": "Bug"}, "type") == ""


def test_receipt_does_not_confuse_the_three_status_columns() -> None:
    """Type, Status and Priority are all STATUS columns with keys status-7/8/9."""
    from boardman.agent.tools.plaky_tools import _labelled_option

    fv = {"status-7": "Bug", "status-8": "Assigned", "status-9": "High"}
    assert _labelled_option(RECEIPT_INDEX, fv, "type") == "Bug"
    assert _labelled_option(RECEIPT_INDEX, fv, "status") == "Assigned"
    assert _labelled_option(RECEIPT_INDEX, fv, "priority") == "High"


# --- the receipt may only name a person the board is actually going to get -------------


def test_receipt_refuses_to_name_an_assignee_the_matcher_would_reject(monkeypatch) -> None:
    """ "Assignee Quinn - Status Assigned" while the column stays empty is the exact
    false claim this whole work order is about."""
    from boardman.agent.tools import plaky_tools

    monkeypatch.setattr(
        plaky_tools,
        "resolve_people_to_field_values",
        lambda **_k: ({}, ["assignee 'Quinn' did not match one person"]),
    )
    monkeypatch.setattr(
        plaky_tools, "infer_plaky_field_keys_from_normalized", lambda _n: {"engineer": "person-5"}
    )
    name, qa, complaints = plaky_tools._resolved_people(
        {"title": "T", "assignee": "Quinn"}, {"fields": []}, {}
    )
    assert name == "" and qa == ""
    assert complaints and "Quinn" in complaints[0]


def test_receipt_names_the_person_who_will_land_on_the_task(monkeypatch) -> None:
    from boardman.agent.tools import plaky_tools

    monkeypatch.setattr(
        plaky_tools, "resolve_people_to_field_values", lambda **_k: ({"person-5": "481106"}, [])
    )
    monkeypatch.setattr(
        plaky_tools, "infer_plaky_field_keys_from_normalized", lambda _n: {"engineer": "person-5"}
    )
    name, _qa, complaints = plaky_tools._resolved_people(
        {"title": "T", "assignee": "ali"}, {"fields": []}, {"481106": "Ali F"}
    )
    assert name == "Ali F" and not complaints


def test_an_explicit_field_value_wins_so_the_typed_name_is_not_claimed(monkeypatch) -> None:
    """The create path lets field_values override the typed name, so the receipt must
    not report the name that was ignored."""
    from boardman.agent.tools import plaky_tools

    monkeypatch.setattr(
        plaky_tools, "resolve_people_to_field_values", lambda **_k: ({"person-5": "481106"}, [])
    )
    monkeypatch.setattr(
        plaky_tools, "infer_plaky_field_keys_from_normalized", lambda _n: {"engineer": "person-5"}
    )
    name, _qa, complaints = plaky_tools._resolved_people(
        {"title": "T", "assignee": "ali", "field_values": {"person-5": "999"}},
        {"fields": []},
        {"481106": "Ali F"},
    )
    assert name == ""
    assert complaints


def test_board_counts_cover_the_whole_board_not_the_shown_slice() -> None:
    """with_owner_count sits next to board-wide totals; computing it over the truncated
    page made the assistant under-report how many tasks have an owner."""
    import json as _json

    from boardman.agent.tools.plaky_tools import _envelope

    items = [
        {
            "id": str(i),
            "title": f"t{i}",
            "fields": [
                {"type": "PERSON", "title": "Assignee", "value": {"assignedUsers": ["481106"]}}
            ],
        }
        for i in range(70)
    ]
    out = _json.loads(_envelope({"ok": True}, items, limit=60, names={"481106": "Ali F"}))
    assert out["returned"] == 60 and out["total"] == 70
    assert out["with_owner_count"] == 70


# --- status and the person written have to agree --------------------------------------


@pytest.mark.asyncio
async def test_a_qa_only_github_assignee_never_produces_an_assigned_status(monkeypatch) -> None:
    """Resolving the id in one place and filtering it in another wrote Assigned onto a
    task whose Assignee column stayed empty."""
    from boardman.services import issue_handler

    async def fake_resolve(_actor):
        return "77"

    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_github_user_to_plaky_user_id", fake_resolve
    )
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: ("", "not a developer"),
    )
    assert await issue_handler._resolve_issue_engineer_id("quinnqa") == ""


@pytest.mark.asyncio
async def test_an_eligible_github_assignee_is_returned(monkeypatch) -> None:
    from boardman.services import issue_handler

    async def fake_resolve(_actor):
        return "481106"

    monkeypatch.setattr(
        "boardman.plaky.dynamic_qa_status.resolve_github_user_to_plaky_user_id", fake_resolve
    )
    monkeypatch.setattr(
        "boardman.assignment.developer_eligibility.filter_developer",
        lambda pid, cfg=None: (pid, ""),
    )
    assert await issue_handler._resolve_issue_engineer_id("Blasted-ctrl") == "481106"


def test_every_type_github_can_produce_lands_on_one_of_the_five() -> None:
    """Requirement 4 governs what GITHUB can put in the Type column, so the check is
    over every canonical type a branch token or a label can produce. "Tests" reached
    Task, which is on the board but not on the agreed list."""
    from boardman.github.pr_signals import _TYPE_BY_TOKEN
    from boardman.plaky.board_schema import select_field_patch_pair_from_schema
    from boardman.plaky.task_tag_vocab import type_field_patch_candidates

    for canon in sorted(set(_TYPE_BY_TOKEN.values())):
        pair = select_field_patch_pair_from_schema(
            BOARD_TYPE_OPTIONS,
            column_name_substrings=("type", "issue type", "category", "kind"),
            value_label_candidates=type_field_patch_candidates(canon),
            exclude_name_substrings=("subtype",),
        )
        written = OPTION_NAMES.get(str(pair[1]), "") if pair else ""
        assert written in ALLOWED_TYPES, f"{canon} writes {written!r}, outside the five"


# --- GitHub listings must not read as the whole picture -------------------------------


@pytest.mark.asyncio
async def test_github_listings_admit_their_page_limit_and_point_at_the_board(monkeypatch) -> None:
    """The model called one page of 30 closed PRs "all 30 PRs" and ruled out work that
    was sitting on the Plaky board the whole time."""
    import json as _json
    from contextlib import asynccontextmanager

    from boardman.agent.tools import github_tools

    class FakeResponse:
        status_code = 200

        def json(self):
            return [
                {"number": i, "title": f"pr {i}", "user": {"login": "x"}, "state": "closed"}
                for i in range(30)
            ]

    class FakeClient:
        async def get(self, *_a, **_k):
            return FakeResponse()

    @asynccontextmanager
    async def fake_client():
        yield FakeClient()

    monkeypatch.setattr(github_tools.settings, "github_pat", "token")
    monkeypatch.setattr(github_tools, "shared_github_client", fake_client)

    out = _json.loads(await github_tools._github_list_pull_requests("o/r", state="all"))
    assert out["truncated"] is True
    assert out["returned"] == 30
    assert "plaky_list_tasks" in out["next_step"]


# --- tier classifier default for unknown repos ----------------------------------------


def test_a_repo_with_no_metadata_defaults_to_tier_2_not_3() -> None:
    """Tier 2 means every QA at tier 2 or higher is eligible. Tier 3 would restrict
    unknown repos to tier-3-only QAs, which is the wrong default when no signal says the
    repo is complex. Raised by sorge on PR #88."""
    from boardman.assignment.tier_classifier import classify_repo_tier

    tier, _score = classify_repo_tier(None)
    assert tier == 2, f"unknown repos should be tier 2, got {tier}"
    tier2, _ = classify_repo_tier({})
    assert tier2 == 2
