"""The neutral behaviour settings accept both the new and the original PLAKY_ env names."""

from __future__ import annotations

import pytest

from boardman.settings import Settings


@pytest.mark.parametrize(
    "attr,names",
    [
        (
            "complete_when_all_prs_merged",
            ("COMPLETE_WHEN_ALL_PRS_MERGED", "PLAKY_COMPLETE_WHEN_ALL_PRS_MERGED"),
        ),
        ("skip_needs_qa_for_draft", ("SKIP_NEEDS_QA_FOR_DRAFT", "PLAKY_SKIP_NEEDS_QA_FOR_DRAFT")),
    ],
)
def test_both_env_names_work_and_the_default_is_true(monkeypatch, attr, names):
    for n in names:
        monkeypatch.delenv(n, raising=False)
    assert getattr(Settings(_env_file=None), attr) is True
    for n in names:
        monkeypatch.setenv(n, "false")
        assert getattr(Settings(_env_file=None), attr) is False, n
        monkeypatch.delenv(n)
