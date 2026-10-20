"""System-prompt notice for the ClickUp provider (the counterpart of plaky_prompt_extra)."""


def clickup_provider_markdown(list_id: str | None, note: str = "") -> str:
    """Tell the model it is working against ClickUp, which tools exist, and the placement."""
    lid = (list_id or "").strip()
    lines = [
        "",
        "## Task provider: ClickUp",
        "",
        "This deployment's task tracker is **ClickUp**, not Plaky. Anywhere the instructions above "
        "mention Plaky or a `plaky_*` tool, use the `clickup_*` equivalent below. A Plaky *board* "
        "is a ClickUp **list**; ids you are given as `board_id` are list ids.",
        "",
        "| Plaky tool | ClickUp tool |",
        "|---|---|",
        "| plaky_list_boards | clickup_list_lists |",
        "| plaky_list_tasks | clickup_list_tasks |",
        "| plaky_get_task | clickup_get_task |",
        "| plaky_list_workspace_users | clickup_list_workspace_users |",
        "| plaky_create_task / plaky_create_tasks | clickup_create_task / clickup_create_tasks |",
        "| plaky_update_task | clickup_update_task |",
        "| plaky_add_comment / plaky_link_prs | clickup_add_comment / clickup_link_prs |",
        "| plaky_create_subtask | clickup_create_subtask |",
        "",
        "ClickUp has **no** board schema, groups or custom-field patching, so there is no equivalent "
        "of plaky_create_tasks_deferred (use clickup_create_tasks, which waits for the writes), plaky_board_schema, plaky_match_board, plaky_match_group, plaky_get_board_item, "
        "plaky_patch_item_fields, plaky_review_board or plaky_save_task_preferences. Skip any "
        "instruction that tells you to call them. Priorities are urgent, high, medium or low. "
        "Assignees are plain names or emails; never pass numeric ids.",
        "",
    ]
    if lid:
        lines.append(
            f"**Current list_id**: `{lid}`. It is already selected; do not ask which list to use."
        )
    else:
        lines.append(
            "**list_id**: not set. Call clickup_list_lists to find one, or ask the user, unless "
            "CLICKUP_DEFAULT_LIST_ID is configured server-side."
        )
    if note.strip():
        lines += ["", note.strip()]
    lines.append("")
    return "\n".join(lines)
