import pytest

from before import Schedule


def test_first_attempt_waits_the_base():
    assert Schedule().delay_for(1) == 1.0


def test_backs_off_exponentially():
    assert Schedule().delay_for(4) == 8.0


def test_is_capped():
    assert Schedule().delay_for(20) == 60.0


def test_rejects_a_zero_attempt():
    with pytest.raises(ValueError):
        Schedule().delay_for(0)


def test_totals_the_waits():
    assert Schedule().total_for(3) == 1.0 + 2.0 + 4.0
