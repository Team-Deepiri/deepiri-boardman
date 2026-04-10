from boardman.plaky.name_match import rank_plaky_rows


def test_exact_board_name():
    boards = [{"id": "1", "name": "Deepiri Main"}, {"id": "2", "name": "Other"}]
    m, best = rank_plaky_rows(boards, "deepiri main")
    assert m[0]["id"] == "1"
    assert best is not None and best["id"] == "1"


def test_substring_query():
    boards = [{"id": "99", "name": "Cyrex AI System"}, {"id": "1", "name": "X"}]
    m, best = rank_plaky_rows(boards, "cyrex")
    assert m[0]["id"] == "99"
    assert best is not None


def test_empty_query_no_best():
    boards = [{"id": "a", "name": "Z"}]
    m, best = rank_plaky_rows(boards, "")
    assert all(x["score"] == 0 for x in m)
    assert best is None


def test_token_overlap():
    boards = [{"id": "1", "name": "AU Delivery"}, {"id": "2", "name": "DM Infra"}]
    m, _ = rank_plaky_rows(boards, "delivery au")
    assert m[0]["id"] == "1"
