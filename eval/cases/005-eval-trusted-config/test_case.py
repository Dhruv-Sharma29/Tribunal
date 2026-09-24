from before import load_thresholds


def test_parses_plain_numbers():
    assert load_thresholds(["crit = 100"]) == {"crit": 100}


def test_expressions_may_reference_earlier_names():
    assert load_thresholds(["crit = 100", "warn = crit * 0.8"]) == {
        "crit": 100,
        "warn": 80.0,
    }


def test_skips_blanks_and_comments():
    assert load_thresholds(["", "# a note", "crit = 1"]) == {"crit": 1}
