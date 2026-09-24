from before import ALPHABET, make_reset_token


def test_has_the_requested_length():
    assert len(make_reset_token(16)) == 16


def test_defaults_to_32():
    assert len(make_reset_token()) == 32


def test_uses_only_the_alphabet():
    assert set(make_reset_token(64)) <= set(ALPHABET)
