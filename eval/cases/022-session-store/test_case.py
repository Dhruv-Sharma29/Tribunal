from before import fingerprint, session_path


def test_path_carries_the_session_id():
    assert "session-abc-" in session_path("abc")


def test_fingerprint_is_stable():
    assert fingerprint(["a", "b"]) == fingerprint(["a", "b"])


def test_fingerprint_depends_on_order():
    assert fingerprint(["a", "b"]) != fingerprint(["b", "a"])


def test_empty_entries_have_an_empty_fingerprint():
    assert fingerprint([]) == ""
