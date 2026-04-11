from boardman.plaky.board_schema import (
    format_board_schema_markdown,
    looks_like_placeholder_plaky_field_key,
    normalize_board_payload,
    validate_field_values_against_board_schema,
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
    assert "In Progress" in names["Status"]["options"]
    assert "Needs QA AGAIN" in names["Status"]["options"]
    assert "Feature" in names["Type"]["options"]


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


def test_validate_field_values_rejects_placeholders():
    msg = validate_field_values_against_board_schema(
        {"person-1": "x", "real_uuid": "y"},
        {"fields": [{"name": "A", "key": "real_uuid", "options": []}]},
    )
    assert msg is not None
    assert "person-1" in msg


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
