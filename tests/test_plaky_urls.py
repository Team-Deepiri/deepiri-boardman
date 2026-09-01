from boardman.plaky.urls import plaky_task_markdown_link, plaky_task_web_url


def test_trusts_matching_hint():
    url = plaky_task_web_url("7332088", "https://app.plaky.com/board/1/task/7332088")
    assert url == "https://app.plaky.com/board/1/task/7332088"


def test_rejects_stale_hint_for_different_task():
    # Hint points at a different item than task_id now refers to (e.g. a
    # relinked/reassigned mapping row) — must not render that mismatched link.
    url = plaky_task_web_url("7332088", "https://app.plaky.com/task/999")
    assert url == "https://app.plaky.com/task/7332088"


def test_builds_real_nested_route_when_board_and_space_known():
    # Plaky's own web app has no top-level /task/{id} route — only the nested
    # spaces/{s}/boards/{b}/views/{v}/items/{i} route resolves. This is the
    # link that must be produced whenever board/space are known.
    url = plaky_task_web_url("7338889", None, board_id="278726", space_id="185467")
    assert url == "https://app.plaky.com/spaces/185467/boards/278726/views/0/items/7338889"


def test_falls_back_to_legacy_guess_without_board_or_space():
    url = plaky_task_web_url("7332088", None)
    assert url == "https://app.plaky.com/task/7332088"


def test_pending_id_returns_hint():
    assert plaky_task_web_url("pending:abc", "") == ""


def test_markdown_link_falls_back_without_url():
    assert plaky_task_markdown_link("pending:abc", None) == "Plaky task `pending:abc`"


def test_markdown_link_with_board_and_space():
    link = plaky_task_markdown_link("7338889", None, board_id="278726", space_id="185467")
    assert link == (
        "[Plaky task 7338889]"
        "(https://app.plaky.com/spaces/185467/boards/278726/views/0/items/7338889)"
    )
