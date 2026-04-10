from boardman.plaky.board_schema import format_board_schema_markdown, normalize_board_payload


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
