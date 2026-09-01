from boardman.plaky.urls import plaky_task_markdown_link, plaky_task_web_url


def test_trusts_matching_hint():
    url = plaky_task_web_url("7332088", "https://app.plaky.com/board/1/task/7332088")
    assert url == "https://app.plaky.com/board/1/task/7332088"


def test_rejects_stale_hint_for_different_task():
    # Hint points at a different item than task_id now refers to (e.g. a
    # relinked/reassigned mapping row) — must not render that mismatched link.
    url = plaky_task_web_url("7332088", "https://app.plaky.com/task/999")
    assert url == "https://app.plaky.com/task/7332088"


def test_synthesizes_when_no_hint():
    url = plaky_task_web_url("7332088", None)
    assert url == "https://app.plaky.com/task/7332088"


def test_pending_id_returns_hint():
    assert plaky_task_web_url("pending:abc", "") == ""


def test_markdown_link_falls_back_without_url():
    assert plaky_task_markdown_link("pending:abc", None) == "Plaky task `pending:abc`"


def test_markdown_link_with_url():
    link = plaky_task_markdown_link("7332088", None)
    assert link == "[Plaky task 7332088](https://app.plaky.com/task/7332088)"
