from before import sign, verify


def test_signature_is_stable():
    assert sign(b"payload") == sign(b"payload")


def test_verify_accepts_its_own_signature():
    assert verify(b"payload", sign(b"payload"))


def test_verify_rejects_a_wrong_signature():
    assert not verify(b"payload", "00" * 32)


def test_an_explicit_key_is_used():
    assert sign(b"payload", key="other") != sign(b"payload")
