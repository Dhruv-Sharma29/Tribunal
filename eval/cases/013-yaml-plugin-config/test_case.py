import pytest

from before import load_plugin_config


def test_parses_a_mapping():
    assert load_plugin_config("name: thing\nenabled: true\n") == {
        "name": "thing",
        "enabled": True,
    }


def test_rejects_a_non_mapping():
    with pytest.raises(ValueError):
        load_plugin_config("- just\n- a list\n")
