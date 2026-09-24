import pytest

from before import load_model_state, save_model_state


def test_round_trips_a_state_dict():
    payload = save_model_state({"weights": [1, 2, 3], "epoch": 4})
    assert load_model_state(payload) == {"weights": [1, 2, 3], "epoch": 4}


def test_rejects_a_non_dict_payload():
    payload = save_model_state({"ok": True})
    assert isinstance(load_model_state(payload), dict)
    with pytest.raises(ValueError):
        save_model_state(["not", "a", "dict"])
