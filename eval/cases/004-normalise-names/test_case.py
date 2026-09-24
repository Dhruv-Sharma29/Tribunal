from before import normalise, normalise_all


def test_strips_and_lowercases():
    assert normalise("  Ana  ") == "ana"


def test_drops_empty_names():
    assert normalise_all([" Ana ", "   ", "BO"]) == ["ana", "bo"]
