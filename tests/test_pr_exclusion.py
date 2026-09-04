from boardman.github.pr_exclusion import pr_sync_exclusion_reason


def test_bot_author_excluded():
    reason = pr_sync_exclusion_reason(
        base_ref="main",
        head_ref="dependabot/npm_and_yarn/foo",
        pr_user={"login": "dependabot[bot]", "type": "Bot"},
    )
    assert reason


def test_dev_to_main_excluded():
    reason = pr_sync_exclusion_reason(
        base_ref="main", head_ref="dev", pr_user={"login": "joe", "type": "User"}
    )
    assert reason


def test_main_to_dev_excluded():
    reason = pr_sync_exclusion_reason(
        base_ref="dev", head_ref="main", pr_user={"login": "joe", "type": "User"}
    )
    assert reason


def test_normal_pr_not_excluded():
    reason = pr_sync_exclusion_reason(
        base_ref="main", head_ref="feat/my-feature", pr_user={"login": "joe", "type": "User"}
    )
    assert reason == ""


def test_feature_branch_to_dev_not_excluded():
    reason = pr_sync_exclusion_reason(
        base_ref="dev", head_ref="feat/my-feature", pr_user={"login": "joe", "type": "User"}
    )
    assert reason == ""
