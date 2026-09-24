from before import extract_ids

LINES = ["id=alpha-1 ok", "noise", "id=beta-2 ok", "id=alpha-3 ok"]


def test_filters_by_prefix():
    assert extract_ids(LINES, "alpha") == ["alpha-1", "alpha-3"]


def test_returns_empty_when_nothing_matches():
    assert extract_ids(LINES, "zzz") == []


def test_ignores_lines_without_an_id():
    assert extract_ids(["nothing here"], "a") == []
