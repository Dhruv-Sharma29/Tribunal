"""Per-provider contracts, checked without a credential or a network call.

What can be asserted offline is the part that is easy to get silently wrong: the capability
each backend claims, the dialect it asks for, how it maps the project's effort levels onto its
own, and how it normalises usage. The wire calls themselves need a key -- `providers.py`
records where each shape was verified, and `test_llm_live.py` exercises whichever backend has
one configured.
"""

from __future__ import annotations

import json

import pytest

from tribunal.config import ProvidersConfig, ProviderSettings, cost_usd, price_for
from tribunal.contracts import Critique
from tribunal.llm import registry
from tribunal.llm.base import ProviderName, StructureMode
from tribunal.llm.gemini_p import _usage as gemini_usage
from tribunal.llm.nim_p import HOSTED_BASE_URL, REASONING_EFFORT, NIMProvider
from tribunal.llm.openai_p import EFFORT_MAP
from tribunal.llm.openai_p import _usage as openai_usage

ALL = list(ProviderName)


@pytest.fixture
def providers():
    return registry.build_all(ProvidersConfig())


# -- the registry ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_every_provider_is_constructible_without_a_credential(name, providers):
    """`doctor` and `providers` must report on every backend without touching a key."""
    provider = providers[name]
    assert provider.name is name
    assert provider.default_model
    assert provider.key_env


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_capabilities_need_no_credential_and_no_network(name, providers):
    caps = providers[name].capabilities(providers[name].default_model)
    assert isinstance(caps.structure, StructureMode)


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_a_missing_key_is_reported_not_raised(name, providers, monkeypatch):
    monkeypatch.delenv(providers[name].key_env, raising=False)
    reason = providers[name].available()
    assert reason is not None
    assert providers[name].key_env in reason


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_every_default_model_has_a_pinned_price(name, providers):
    """An unpriced default would make the first cost number in every trace wrong."""
    price = price_for(providers[name].default_model)
    assert price.provider is name


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_every_provider_records_where_its_wire_shape_came_from(name, providers):
    assert providers[name].contract_source != "unverified"


@pytest.mark.parametrize("name", ALL, ids=lambda n: n.value)
def test_every_provider_adapts_the_critique_schema(name, providers):
    adapted = providers[name].adapt_schema(Critique.model_json_schema())
    assert adapted["type"] == "object"
    json.dumps(adapted)


# -- what each backend actually guarantees ---------------------------------------------------


def test_structure_guarantees_are_not_uniform(providers):
    """The whole reason `StructureMode` exists. A backend that can only be *asked* for JSON is
    a different proposition from one that constrains decoding, and an eval that mixes them
    without recording which is which cannot interpret its own parse-retry rate."""
    modes = {
        name: providers[name].capabilities(providers[name].default_model).structure
        for name in ALL
    }
    assert modes[ProviderName.ANTHROPIC] is StructureMode.NATIVE_STRICT
    assert modes[ProviderName.OPENAI] is StructureMode.NATIVE_STRICT
    assert modes[ProviderName.GEMINI] is StructureMode.NATIVE_SCHEMA
    assert modes[ProviderName.NIM] is StructureMode.NATIVE_JSON


def test_only_anthropic_offers_explicit_prompt_cache_breakpoints(providers):
    """Everyone else caches implicitly, which needs no request-side support -- so there is
    nothing for the client to arrange."""
    explicit = [
        name
        for name in ALL
        if providers[name].capabilities(providers[name].default_model).explicit_prompt_cache
    ]
    assert explicit == [ProviderName.ANTHROPIC]


def test_nim_does_not_report_cache_tokens(providers):
    """docs/06 makes a zero cache-read the signal for a silent prefix invalidator. On NIM that
    diagnostic is simply unavailable, and claiming otherwise would mislead."""
    assert not providers[ProviderName.NIM].capabilities("nvidia/x").reports_cache_tokens


# -- effort mapping --------------------------------------------------------------------------


def test_openai_clamps_the_project_effort_levels():
    """OpenAI has no xhigh/max. The Arbiter runs at xhigh by default, so clamping beats a 400
    and beats silently dropping the parameter."""
    assert EFFORT_MAP["xhigh"] == "high"
    assert EFFORT_MAP["max"] == "high"
    assert EFFORT_MAP["low"] == "low"


def test_nim_clamps_the_project_effort_levels():
    assert REASONING_EFFORT["max"] == "high"
    assert set(REASONING_EFFORT) == {"low", "medium", "high", "xhigh", "max"}


# -- NIM hosted vs self-hosted ---------------------------------------------------------------


def test_hosted_nim_is_detected_from_the_base_url():
    hosted = NIMProvider()
    self_hosted = NIMProvider(
        ProviderSettings(key_env="NVIDIA_API_KEY", base_url="http://localhost:8000/v1")
    )
    assert hosted.is_hosted
    assert hosted.settings.base_url == HOSTED_BASE_URL
    assert not self_hosted.is_hosted


def test_inline_reasoning_is_claimed_only_for_self_hosted_nim():
    """Measured, not assumed. NVIDIA's docs describe reasoning inline in `content`, but the
    hosted endpoint returns it in a separate `reasoning_content` field. A self-hosted build
    may match the docs, so the extraction path stays -- it is just not the hosted default."""
    model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    assert not NIMProvider().capabilities(model).inline_reasoning
    self_hosted = NIMProvider(
        ProviderSettings(key_env="NVIDIA_API_KEY", base_url="http://localhost:8000/v1")
    )
    assert self_hosted.capabilities(model).inline_reasoning


def test_a_non_reasoning_nim_model_claims_no_effort_control():
    assert not NIMProvider().capabilities("meta/llama-3.3-70b-instruct").controllable_effort


# -- usage normalisation ---------------------------------------------------------------------


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_openai_cached_tokens_are_not_billed_twice():
    """OpenAI reports cached tokens *inside* `input_tokens`, unlike Anthropic where they are a
    separate bucket. Adding them would overstate every cached call."""
    response = _Obj(
        id="resp_1",
        usage=_Obj(
            input_tokens=1000,
            output_tokens=200,
            input_tokens_details=_Obj(cached_tokens=800),
        ),
    )
    usage = openai_usage("gpt-6-astra", response)
    assert usage.input_tokens == 200  # 1000 total - 800 cached
    assert usage.cache_read_input_tokens == 800
    expected = cost_usd("gpt-6-astra", 200, 200, 800, 0)
    assert usage.cost_usd == expected


def test_gemini_thinking_tokens_bill_as_output():
    """Omitting them would under-report the cost of exactly the models the tribunal wants for the
    Arbiter."""
    interaction = _Obj(
        id="int_1",
        usage=_Obj(
            prompt_token_count=1000,
            response_token_count=100,
            thoughts_token_count=400,
            cached_content_token_count=200,
        ),
    )
    usage = gemini_usage("gemini-3.8-flash", interaction)
    assert usage.output_tokens == 500  # 100 response + 400 thinking
    assert usage.input_tokens == 800
    assert usage.cache_read_input_tokens == 200


def test_missing_usage_fields_do_not_crash_the_run():
    """A provider that omits a usage field should cost 0 for that term, not take the round
    down -- the critique is already in hand by then."""
    assert openai_usage("gpt-6-astra", _Obj(id="x", usage=None)).output_tokens == 0
    assert gemini_usage("gemini-3.8-flash", _Obj(id="x", usage=None)).input_tokens == 0


def test_nim_costs_zero_legitimately():
    """Marked `not_token_metered`, which is a distinct state from an unpinned price."""
    model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    assert price_for(model).billing == "not_token_metered"
    assert cost_usd(model, 10_000, 5_000) == 0.0


def test_an_unpriced_model_still_raises():
    with pytest.raises(KeyError, match="no price pinned"):
        price_for("nvidia/some-unreleased-model")


# -- provider-qualified pricing --------------------------------------------------------------


def test_the_price_table_covers_every_provider():
    from tribunal.config import PRICES

    covered = {price.provider for price in PRICES.values()}
    assert covered == set(ALL)


def test_anthropic_model_ids_carry_no_date_suffix():
    """The Anthropic API rejects date-suffixed ids outright. The dated form survives only as
    an alias, which is the opposite of how this was first written."""
    from tribunal.config import PRICES, resolve_model

    for model, price in PRICES.items():
        if price.provider is ProviderName.ANTHROPIC:
            assert not model[-1].isdigit() or "-20" not in model, model
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"


def test_an_expired_introductory_rate_warns():
    """A cost row computed from a silently-expired rate is a number nobody can defend."""
    from datetime import date

    from tribunal.config import ModelPrice

    price = ModelPrice(
        provider=ProviderName.GEMINI, input_per_mtok=0.75, output_per_mtok=3.75,
        intro_until="2026-12-31", standard_after_intro=(1.50, 7.50),
    )
    assert not price.intro_expired(date(2026, 9, 16))
    assert price.intro_expired(date(2027, 1, 1))


def test_gemini_intro_pricing_records_the_post_intro_rate():
    """Google publishes the post-intro rate up front, so it is pinned rather than discovered
    when the bill doubles."""
    price = price_for("gemini-3.8-flash")
    assert price.standard_after_intro == (1.50, 7.50)
    assert price.intro_until == "2026-12-31"
