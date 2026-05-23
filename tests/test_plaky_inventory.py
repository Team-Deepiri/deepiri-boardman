from boardman.plaky.inventory import compact_field, compact_option, field_looks_status_like


def test_compact_option_keeps_ids_and_display_name():
    assert compact_option(
        {
            "id": "opt-1",
            "optionId": "alt-1",
            "name": "Needs QA",
            "color": "#ffcc00",
            "extra": "ignored",
        }
    ) == {
        "id": "opt-1",
        "optionId": "alt-1",
        "name": "Needs QA",
        "color": "#ffcc00",
    }


def test_compact_field_keeps_key_type_and_options():
    field = compact_field(
        {
            "key": "status-1",
            "name": "QA Status",
            "type": "STATUS",
            "options": [
                {"id": "ready", "name": "Ready"},
                "Done",
                "",
            ],
        }
    )

    assert field["key"] == "status-1"
    assert field["name"] == "QA Status"
    assert field["type"] == "STATUS"
    assert field["options"] == [{"id": "ready", "name": "Ready"}, {"name": "Done"}]


def test_field_looks_status_like_by_name_or_type():
    assert field_looks_status_like({"name": "QA Status", "type": ""}) is True
    assert field_looks_status_like({"name": "Workflow", "type": "STATUS"}) is True
    assert field_looks_status_like({"name": "Engineer", "type": "PERSON"}) is False
