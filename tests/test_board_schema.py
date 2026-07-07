from boardman.plaky.board_schema import (
    format_board_schema_markdown,
    looks_like_placeholder_plaky_field_key,
    match_repo_tokens_to_plaky_tag_option_values,
    normalize_board_payload,
    plaky_repo_field_value_format,
    resolve_repo_tag_field_values_from_schema,
    validate_field_values_against_board_schema,
    validate_field_values_detailed,
)


def test_normalize_board_payload_fields_and_groups():
    board = {
        "name": "Boardman Test Board",
        "itemFields": [
            {
                "name": "Status",
                "type": "status",
                "options": [{"name": "In Progress"}, {"name": "Needs QA AGAIN"}],
            },
            {"name": "Type", "type": "select", "choices": ["Feature", "Bug"]},
        ],
    }
    groups = [{"id": "g1", "name": "Boardman"}, {"id": "g2", "name": "Bogeyman"}]
    n = normalize_board_payload(board, groups)
    assert n["board_name"] == "Boardman Test Board"
    assert len(n["groups"]) == 2
    names = {f["name"]: f for f in n["fields"]}
    assert "Status" in names
    status_opts = names["Status"]["options"]
    opt_labels = [o if isinstance(o, str) else (o.get("name") or "") for o in status_opts]
    assert "In Progress" in opt_labels
    assert "Needs QA AGAIN" in opt_labels
    type_opts = names["Type"]["options"]
    type_labels = [o if isinstance(o, str) else (o.get("name") or "") for o in type_opts]
    assert "Feature" in type_labels


def test_normalize_status_options_from_configuration_values():
    """Plaky STATUS fields nest options under configuration.values (key=id, title=label).

    Regression: before this was handled, every status field normalized to 0 options, which
    silently disabled ALL PR→Plaky status automation.
    """
    board = {
        "name": "diri-cyrex",
        "itemFields": [
            {
                "name": "Status",
                "type": "STATUS",
                "key": "status-6",
                "configuration": {
                    "values": [
                        {"key": "0", "title": "NEEDS ASSIGNED", "color": "#aaa"},
                        {"key": "8", "title": "Assigned"},
                        {"key": "5", "title": "In QA"},
                        {"key": "6", "title": "QA Verified"},
                    ],
                    "defaultValue": "0",
                },
            }
        ],
    }
    n = normalize_board_payload(board, [])
    status = next(f for f in n["fields"] if f["name"] == "Status")
    by_label = {o["name"]: o for o in status["options"]}
    assert set(by_label) == {"NEEDS ASSIGNED", "Assigned", "In QA", "QA Verified"}
    # The option id must mirror the Plaky `key` so PATCH value selection + intent scoring work.
    assert str(by_label["Assigned"]["id"]) == "8"
    assert str(by_label["QA Verified"]["id"]) == "6"


def test_format_board_schema_markdown_includes_options():
    md = format_board_schema_markdown(
        "bid-1",
        ok=True,
        normalized={
            "board_name": "T",
            "groups": [{"id": "g", "name": "Sprint 2"}],
            "fields": [{"name": "Priority", "type": "", "key": "", "options": ["Low", "High"]}],
        },
    )
    assert "bid-1" in md
    assert "Sprint 2" in md
    assert "Priority" in md
    assert "Low" in md


def test_looks_like_placeholder_plaky_field_key():
    assert looks_like_placeholder_plaky_field_key("person-1") is True
    assert looks_like_placeholder_plaky_field_key("STATUS-2") is True
    assert looks_like_placeholder_plaky_field_key("abc123-real-key") is False


def test_validate_field_values_rejects_placeholder_keys_not_on_board():
    msg = validate_field_values_against_board_schema(
        {"person-99": "x", "real_uuid": "y"},
        {"fields": [{"name": "A", "key": "real_uuid", "options": []}]},
    )
    assert msg is not None
    assert "person-99" in msg


def test_validate_field_values_allows_native_plaky_key_pattern_when_on_board():
    assert (
        validate_field_values_against_board_schema(
            {"person-1": "291493", "tag-2": "Team-Deepiri/x"},
            {
                "fields": [
                    {"name": "Contributor", "key": "person-1", "type": "PERSON"},
                    {"name": "Repos", "key": "tag-2", "type": "TAG"},
                ]
            },
        )
        is None
    )


def test_select_field_patch_pair_fallback_when_status_has_no_options():
    from boardman.plaky.board_schema import select_field_patch_pair_from_schema

    norm = {
        "fields": [
            {"name": "Status", "key": "status-1", "type": "STATUS", "options": []},
        ]
    }
    pair = select_field_patch_pair_from_schema(
        norm,
        column_name_substrings=("status",),
        value_label_candidates=("in progress", "doing"),
    )
    assert pair == ("status-1", "in progress")


def test_plaky_repo_field_value_format_short_only_for_tag_columns():
    norm = {
        "fields": [
            {"name": "Repo text", "key": "fld-1", "type": "TEXT"},
            {"name": "Repos tag", "key": "tag-2", "type": "TAG"},
        ]
    }
    assert plaky_repo_field_value_format(norm, "fld-1") == "full"
    assert plaky_repo_field_value_format(norm, "tag-2") == "short"


def test_plaky_repo_field_value_format_native_tag_key_without_schema():
    assert plaky_repo_field_value_format(None, "tag-2") == "short"
    assert plaky_repo_field_value_format({"fields": []}, "tag-2") == "short"
    assert plaky_repo_field_value_format(None, "fld_repo") == "full"


def test_match_repo_tokens_to_plaky_tag_option_values_case_insensitive():
    field = {
        "name": "GitHub Repos",
        "key": "tag-2",
        "type": "TAG",
        "options": [
            {"name": "Team-Deepiri/deepiri-platform", "id": 42},
            {"name": "Other/Thing", "id": 7},
        ],
    }
    matched, unmatched = match_repo_tokens_to_plaky_tag_option_values(
        field, ["team-deepiri/deepiri-platform"]
    )
    assert matched == [42]
    assert unmatched == []


def test_match_repo_tokens_accepts_repo_short_name_without_owner():
    field = {
        "name": "GitHub Repos",
        "key": "tag-2",
        "type": "TAG",
        "options": [{"name": "deepiri-platform", "id": 7}],
    }
    matched, unmatched = match_repo_tokens_to_plaky_tag_option_values(field, ["deepiri-platform"])
    assert matched == [7]
    assert unmatched == []


def test_resolve_repo_tag_field_values_from_schema_rewrites_string_to_ids():
    norm = {
        "fields": [
            {
                "name": "GitHub Repos",
                "key": "tag-2",
                "type": "TAG",
                "options": [{"name": "Team-Deepiri/deepiri-platform", "id": 99}],
            },
        ]
    }
    fv = {"tag-2": "Team-Deepiri/deepiri-platform"}
    resolve_repo_tag_field_values_from_schema(fv, norm, keys={"tag-2"})
    assert fv["tag-2"] == [99]


def test_validate_field_values_unknown_keys_when_schema_has_keys():
    msg = validate_field_values_against_board_schema(
        {"bad_key": "1"},
        {"fields": [{"name": "Status", "key": "fld_status", "options": ["Open"]}]},
    )
    assert msg is not None
    assert "bad_key" in msg
    assert "fld_status" in msg


def test_validate_field_values_ok_when_keys_match():
    assert (
        validate_field_values_against_board_schema(
            {"fld_status": "Open"},
            {"fields": [{"name": "Status", "key": "fld_status", "options": ["Open"]}]},
        )
        is None
    )


def _priority_schema():
    return {
        "fields": [
            {
                "name": "Priority",
                "key": "priority-key",
                "options": ["High", "Medium", "Low"],
            }
        ]
    }


def test_validate_field_values_detailed_rejects_bad_option():
    cleaned, errors, warnings = validate_field_values_detailed(
        {"priority-key": "urgent"},
        _priority_schema(),
        options_check=True,
    )
    assert cleaned == {}
    assert errors and len(errors) == 1
    assert "priority-key" in errors[0]
    assert "urgent" in errors[0]
    assert "High" in errors[0] and "Medium" in errors[0] and "Low" in errors[0]
    assert warnings == []


def test_validate_field_values_detailed_normalizes_option_case():
    cleaned, errors, warnings = validate_field_values_detailed(
        {"priority-key": "high"},
        _priority_schema(),
        options_check=True,
    )
    assert cleaned == {"priority-key": "High"}
    assert errors == []
    assert warnings == []


def test_validate_field_values_detailed_passes_keys_when_no_options():
    schema = {
        "fields": [
            {"name": "Notes", "key": "notes-key"},
            {"name": "Priority", "key": "priority-key", "options": ["High", "Low"]},
        ]
    }
    cleaned, errors, warnings = validate_field_values_detailed(
        {"notes-key": "free text", "priority-key": "low"},
        schema,
        options_check=True,
    )
    assert cleaned == {"notes-key": "free text", "priority-key": "Low"}
    assert errors == []
    assert warnings == []


def test_validate_field_values_detailed_warns_when_schema_fetch_failed():
    cleaned, errors, warnings = validate_field_values_detailed(
        {"priority-key": "High"},
        _priority_schema(),
        options_check=True,
        schema_fetch_ok=False,
        schema_fetch_message="upstream 502",
    )
    assert cleaned == {"priority-key": "High"}
    assert errors == []
    assert warnings and "schema bundle returned warning" in warnings[0]
    assert "upstream 502" in warnings[0]


def test_validate_field_values_detailed_rejects_unknown_key():
    cleaned, errors, warnings = validate_field_values_detailed(
        {"made-up": "x"},
        _priority_schema(),
        options_check=True,
    )
    assert cleaned == {}
    assert errors and "made-up" in errors[0]
    assert "priority-key" in errors[0]
    assert warnings == []
