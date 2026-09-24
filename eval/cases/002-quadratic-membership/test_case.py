from before import select_active, summarise

RECORDS = [
    {"id": 1, "name": "ana"},
    {"id": 2, "name": "bo"},
    {"id": 3, "name": "cy"},
]


def test_selects_in_input_order():
    assert [r["id"] for r in select_active(RECORDS, [3, 1])] == [1, 3]


def test_summarise_joins_names():
    assert summarise(RECORDS, [1, 2]) == "ana, bo"
