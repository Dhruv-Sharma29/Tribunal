from before import summarise

ROWS = [{"a": 1, "b": 2}, {"a": 3}]


def test_uses_only_the_requested_columns():
    assert summarise(ROWS, ["a"]) == ["1", "3"]


def test_missing_values_become_empty():
    assert summarise(ROWS, ["a", "b"]) == ["1 | 2", "3 | "]


def test_no_columns_gives_empty_lines():
    assert summarise(ROWS, []) == ["", ""]
