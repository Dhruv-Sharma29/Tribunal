from before import hash_password, verify

SALT = b"\x01\x02\x03\x04"


def test_is_deterministic():
    assert hash_password("hunter2", SALT) == hash_password("hunter2", SALT)


def test_differs_by_salt():
    assert hash_password("hunter2", SALT) != hash_password("hunter2", b"\x09" * 4)


def test_verify_accepts_the_right_password():
    assert verify("hunter2", SALT, hash_password("hunter2", SALT))


def test_verify_rejects_the_wrong_password():
    assert not verify("nope", SALT, hash_password("hunter2", SALT))
