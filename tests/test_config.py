"""Configuration and the price table.

The price table earns a test because `cost_usd` is computed at trace-write time and every
eval number downstream is denominated in it. A silently-zero cost would make the cost row of
the results table wrong in a way nobody notices.
"""

from __future__ import annotations

import pytest

from tribunal.config import (
    PRICE_TABLE_VERSION,
    PRICES,
    BudgetConfig,
    GroundingConfig,
    PolicyConfig,
    Settings,
    cost_usd,
    price_for,
    resolve_model,
)
from tribunal.contracts import Dimension, Severity


def test_defaults_match_the_documented_decision_table():
    policy = PolicyConfig()
    assert policy.max_rounds == 3
    assert policy.patch_attempts_per_round == 2
    assert policy.accept_threshold == 4.0
    assert policy.no_progress_epsilon == 2.0
    assert policy.max_hunks == 8


def test_the_accept_threshold_tolerates_exactly_one_medium():
    """Not zero, on purpose: a system requiring zero open issues never terminates on real
    code. 4.0 admits one full-confidence MEDIUM, or four LOWs."""
    assert Severity.MEDIUM.weight * 1.0 <= PolicyConfig().accept_threshold
    assert Severity.HIGH.weight * 1.0 > PolicyConfig().accept_threshold


def test_the_hard_block_is_asymmetric_and_says_so_in_config():
    """Security HIGH auto-rejects; performance HIGH does not. An asymmetric threshold is a
    statement of values, and stating it in config beats pretending the system is neutral."""
    rules = PolicyConfig().hard_block
    assert [r.dimension for r in rules] == [Dimension.SECURITY]
    assert rules[0].severity is Severity.HIGH
    assert rules[0].min_confidence == 0.6


def test_budget_defaults_sit_above_the_documented_worst_case():
    """docs/10 puts a three-round escalate at ~$0.82; the cap is ~2.5x that."""
    assert BudgetConfig().max_usd == 2.00
    assert BudgetConfig().max_wall_seconds == 600


def test_execution_is_off_by_default():
    assert Settings().sandbox.allow_exec is False
    assert Settings().sandbox.mode == "subprocess"


# -- Pricing ---------------------------------------------------------------------------------


def test_one_round_costs_what_the_estimate_says():
    """docs/10 estimates ~24K in / ~4.9K out per round at ~$0.24."""
    assert cost_usd("claude-opus-5", 24_000, 4_900) == pytest.approx(0.24, abs=0.01)


def test_caching_is_visible_in_the_cost():
    """If a cached round does not come out cheaper, the cost model is not modelling caching --
    and cache effectiveness is the thing docs/06 says you will otherwise only notice on the
    bill."""
    uncached = cost_usd("claude-opus-5", 24_000, 4_900)
    cached = cost_usd("claude-opus-5", 2_000, 4_900, cache_read_input_tokens=22_000)
    assert cached < uncached
    assert cached == pytest.approx(0.1435)


def test_a_cache_write_costs_more_than_a_plain_read():
    assert cost_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=10_000) > cost_usd(
        "claude-opus-5", 10_000, 0
    )


def test_an_unpriced_model_raises_rather_than_costing_zero():
    """A silent 0.0 would make every published cost number quietly wrong, which is worse than
    a crash on the first request."""
    with pytest.raises(KeyError, match="no price pinned"):
        price_for("claude-imaginary-9")


def test_aliases_resolve_to_the_canonical_model_id():
    """This test used to assert the opposite, and it was wrong.

    The Anthropic API rejects date-suffixed model ids, so `claude-haiku-4-5` is canonical and
    the dated form is the alias -- not the other way round.
    """
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"
    assert price_for("claude-haiku-4-5").input_per_mtok == 1.00
    assert price_for("claude-haiku-4-5-20251001").input_per_mtok == 1.00


def test_a_pinned_context_window_is_positive_when_published():
    """`context` is None where the provider does not publish one. That is honest; a made-up
    number would be worse than an absent one."""
    assert all(p.context is None or p.context > 0 for p in PRICES.values())
    assert PRICES["claude-opus-5"].context == 1_000_000


# -- Loading and snapshotting ----------------------------------------------------------------


def test_toml_overrides_defaults(tmp_path):
    path = tmp_path / "tribunal.toml"
    path.write_text('[policy]\nmax_rounds = 5\naccept_threshold = 1.5\n', encoding="utf-8")
    settings = Settings.load(path)
    assert settings.policy.max_rounds == 5
    assert settings.policy.accept_threshold == 1.5
    assert settings.policy.patch_attempts_per_round == 2  # untouched default survives


def test_the_snapshot_stamps_the_price_table_version():
    """The trace header must be interpretable after prices change."""
    snapshot = Settings().snapshot()
    assert snapshot["price_table_version"] == PRICE_TABLE_VERSION
    assert snapshot["policy"]["accept_threshold"] == 4.0


def test_the_snapshot_is_json_serialisable():
    import json

    json.dumps(Settings().snapshot())


def test_an_unknown_grounding_tool_fails_at_construction():
    """Better than discovering at round 1 that a configured critic has no evidence source."""
    from tribunal.grounding.suite import GroundingSuite

    with pytest.raises(ValueError, match="unknown static grounding tools"):
        GroundingSuite(Settings(grounding=GroundingConfig(static_tools=("bandit", "flake8"))))


def test_an_invalid_radon_rank_fails_loudly():
    from tribunal.grounding.radon_t import RadonTool

    with pytest.raises(ValueError, match="min_rank"):
        RadonTool(min_rank="Z")
