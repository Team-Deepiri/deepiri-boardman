"""Tunable limits live in settings.py, once.

Every one of these numbers used to appear twice: as the Field default in settings.py and
again as a literal fallback at the call site (`... or 16`). The two drifted -- the context
budget setting defaulted to 20000 while the module that read it fell back to 24000, so the
configured value and the actual value disagreed whenever the setting was cleared. These
tests pin the numbers to one definition (Sorge review, PR #88).
"""

from __future__ import annotations

import pytest

from boardman import settings as settings_mod
from boardman.settings import (
    DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES,
    DEFAULT_AGENT_PREAMBLE_MAX_CHARS,
    DEFAULT_GITHUB_CODE_SEARCH_MAX_BYTES_PER_FILE,
    DEFAULT_GITHUB_CODE_SEARCH_MAX_FILES,
    DEFAULT_GITHUB_ORG_ACTIVITY_SPLIT_TOP_N,
    DEFAULT_GITHUB_PR_MAX_BODY_CHARS,
    DEFAULT_GITHUB_PR_MAX_FILES,
    DEFAULT_LLM_CONTEXT_BUDGET_CHARS,
    Settings,
)

# (settings field, the module-level constant that must define it)
PAIRS = [
    ("llm_context_budget_chars", DEFAULT_LLM_CONTEXT_BUDGET_CHARS),
    ("github_pr_max_files", DEFAULT_GITHUB_PR_MAX_FILES),
    ("github_pr_max_body_chars", DEFAULT_GITHUB_PR_MAX_BODY_CHARS),
    ("github_code_search_max_files", DEFAULT_GITHUB_CODE_SEARCH_MAX_FILES),
    ("github_code_search_max_bytes_per_file", DEFAULT_GITHUB_CODE_SEARCH_MAX_BYTES_PER_FILE),
    ("github_org_activity_split_top_n", DEFAULT_GITHUB_ORG_ACTIVITY_SPLIT_TOP_N),
    ("agent_org_roster_max_names", DEFAULT_AGENT_ORG_ROSTER_MAX_NAMES),
    ("agent_preamble_max_chars", DEFAULT_AGENT_PREAMBLE_MAX_CHARS),
]


# The values these knobs had BEFORE they were extracted into named constants. Moving a
# literal into settings.py must not change what it is, and "the Field default equals the
# constant" cannot catch that on its own -- both sides move together.
PRE_EXTRACTION_VALUES = {
    "llm_context_budget_chars": 24_000,
    "github_pr_max_files": 40,
    "github_pr_max_body_chars": 4_000,
    "github_code_search_max_files": 16,
    "github_code_search_max_bytes_per_file": 120_000,
}


@pytest.mark.parametrize(("field", "constant"), PAIRS)
def test_the_field_default_is_the_shared_constant(field: str, constant: int) -> None:
    assert Settings.model_fields[field].default == constant


@pytest.mark.parametrize(("field", "expected"), sorted(PRE_EXTRACTION_VALUES.items()))
def test_extracting_a_literal_did_not_change_its_value(field: str, expected: int) -> None:
    assert Settings.model_fields[field].default == expected


# Most limits treat 0 as "unset". github_org_activity_split_top_n does not: 0 there means
# "make no extra GitHub calls", so its unset signal is a negative number.
UNSET_VALUE = {"github_org_activity_split_top_n": -1}


@pytest.mark.parametrize(("field", "constant"), PAIRS)
def test_clearing_the_setting_falls_back_to_the_same_number(
    monkeypatch, field: str, constant: int
) -> None:
    """Unset must mean the documented default -- not a second number hidden at a call site."""
    monkeypatch.setattr(settings_mod.settings, field, UNSET_VALUE.get(field, 0))
    assert _effective(field) == constant


@pytest.mark.parametrize(("field", "constant"), PAIRS)
def test_a_negative_value_never_slices_from_the_wrong_end(
    monkeypatch, field: str, constant: int
) -> None:
    """.env.example teaches -1 for one of these knobs; on the others it must not mean
    `shorts[:-1]`, which drops a real repo while the prompt calls the list complete."""
    monkeypatch.setattr(settings_mod.settings, field, -1)
    effective = _effective(field)
    assert effective > 0
    if field != "github_org_activity_split_top_n":
        assert effective == constant


def test_zero_split_top_is_honoured_rather_than_read_as_unset(monkeypatch) -> None:
    """The setting exists to cap GitHub calls, so it has to be able to reach zero."""
    from boardman.github.org_activity import _split_top_n

    monkeypatch.setattr(settings_mod.settings, "github_org_activity_split_top_n", 0)
    assert _split_top_n() == 0


def _effective(field: str) -> int:
    """Read the limit the way production code reads it."""
    from boardman.agent.org_roster import _max_names
    from boardman.agent.runner import _preamble_max_chars
    from boardman.agent.tools.github_tools import _context_budget
    from boardman.github.code_search import _max_bytes_per_file, _max_files
    from boardman.github.org_activity import _split_top_n
    from boardman.github.pr_review_context import _max_body_chars as _pr_max_body_chars
    from boardman.github.pr_review_context import _max_files as _pr_max_files

    readers = {
        "llm_context_budget_chars": _context_budget,
        "github_code_search_max_files": _max_files,
        "github_code_search_max_bytes_per_file": _max_bytes_per_file,
        "github_org_activity_split_top_n": _split_top_n,
        "agent_org_roster_max_names": _max_names,
        "agent_preamble_max_chars": _preamble_max_chars,
        "github_pr_max_files": _pr_max_files,
        "github_pr_max_body_chars": _pr_max_body_chars,
    }
    return readers[field]()


@pytest.mark.parametrize(("field", "_constant"), PAIRS)
def test_a_configured_value_wins_over_the_default(monkeypatch, field: str, _constant: int) -> None:
    monkeypatch.setattr(settings_mod.settings, field, 7)
    assert _effective(field) == 7


def test_the_preamble_guard_uses_the_configured_length(monkeypatch) -> None:
    """The 600-char ceiling is a tuning knob, so a deployment can move it."""
    from boardman.agent import runner

    promise = "Let me fetch that for you now."
    assert runner._looks_like_unfulfilled_preamble(promise) is True

    monkeypatch.setattr(settings_mod.settings, "agent_preamble_max_chars", 10)
    assert (
        runner._looks_like_unfulfilled_preamble(promise) is False
    ), "a reply longer than the ceiling is treated as substantive"


def test_the_org_roster_cap_is_configurable(monkeypatch) -> None:
    from boardman.agent.org_roster import _max_names

    monkeypatch.setattr(settings_mod.settings, "agent_org_roster_max_names", 5)
    assert _max_names() == 5
