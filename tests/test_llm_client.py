"""The provider-agnostic client: extraction, repair, cassettes, retries, accounting.

Every test here runs against a fake provider or a recorded cassette. Zero API calls, by
construction -- which is the property docs/09-roadmap.md Phase 2 requires and which is harder
to hold with four providers than with one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tribunal.config import LLMConfig, ProvidersConfig, ProviderSettings, Settings
from tribunal.contracts import Critique, Usage
from tribunal.llm import schema as S
from tribunal.llm.base import (
    Capabilities,
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderBadResponse,
    ProviderName,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTransientError,
    StructureMode,
)
from tribunal.llm.cassette import CassetteMiss, CassetteStore, request_hash
from tribunal.llm.client import LLMClient, SchemaRepairFailed, _quantise_in_place
from tribunal.llm.extract import find_json_object, parse_json_payload, split_reasoning

VALID_CRITIQUE = {
    "dimension": "security",
    "round": 1,
    "verdict": "block",
    "issues": [
        {
            "id": "SEC-0000000001",
            "dimension": "security",
            "severity": "high",
            "title": "shell=True on concatenated input",
            "explanation": "The command string is built from a parameter.",
            "evidence": [
                {"kind": "tool_finding", "ref": "e5376668fa", "excerpt": "bandit B602"}
            ],
            "confidence": 0.9,
            "introduced_by_patch": True,
            "suggested_direction": "pass an argv list",
        }
    ],
    "positive_notes": [],
    "tools_consulted": ["bandit", "ruff"],
    "summary": "One command injection.",
}


@dataclass
class FakeProvider(Provider):
    """A scripted provider. Each entry is a payload to return or an exception to raise."""

    name: ProviderName = ProviderName.NIM
    key_env: str = "FAKE_KEY"
    default_model: str = "fake-1"
    contract_source: str = "test double"
    script: list[Any] = field(default_factory=list)
    seen: list[LLMRequest] = field(default_factory=list)
    structure: StructureMode = StructureMode.NATIVE_JSON

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            structure=self.structure,
            explicit_prompt_cache=False,
            reports_cache_tokens=False,
            controllable_effort=True,
            inline_reasoning=False,
        )

    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        return S.for_nim(schema).schema

    def available(self) -> str | None:
        return None

    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        self.seen.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        data, recovered, reasoning = parse_json_payload(text)
        return LLMResponse(
            data=data,
            raw_text=text,
            structure=StructureMode.EXTRACTED if recovered else self.structure,
            usage=Usage(
                model=request.model, input_tokens=100, output_tokens=50,
                cost_usd=0.001, request_id="fake-req",
            ),
            stop_reason="stop",
            reasoning=reasoning,
        )


def client_with(fake: FakeProvider, tmp_path: Path, **llm: Any) -> LLMClient:
    settings = Settings(llm=LLMConfig(cassette_dir=tmp_path, **llm))
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return client


async def call(client: LLMClient, **kw: Any):
    defaults = dict(
        provider=ProviderName.NIM, model="fake-1", system="sys", user="usr",
        output_model=Critique, max_tokens=4000, effort="high",
    )
    return await client.call(**{**defaults, **kw})


# -- the happy path --------------------------------------------------------------------------


async def test_a_valid_payload_validates_without_a_retry(tmp_path):
    fake = FakeProvider(script=[VALID_CRITIQUE])
    outcome = await call(client_with(fake, tmp_path))
    assert isinstance(outcome.value, Critique)
    assert outcome.repair_retries == 0
    assert outcome.local_repairs == []
    assert outcome.value.verdict == "block"


async def test_the_request_carries_the_adapted_schema(tmp_path):
    fake = FakeProvider(script=[VALID_CRITIQUE])
    await call(client_with(fake, tmp_path))
    sent = fake.seen[0]
    assert sent.schema_name == "Critique"
    # NIM gets the OpenAI shape: every key required, additionalProperties false.
    assert set(sent.schema_["required"]) == set(sent.schema_["properties"])
    assert "multipleOf" not in json.dumps(sent.schema_)


async def test_dropped_constraints_are_reported_on_the_outcome(tmp_path):
    """The caller has to know which guarantees are validation-only."""
    outcome = await call(client_with(FakeProvider(script=[VALID_CRITIQUE]), tmp_path))
    paths = [p for p in outcome.dropped_constraints if p.endswith("confidence")]
    assert paths and "multipleOf" in outcome.dropped_constraints[paths[0]]


# -- the free local repair -------------------------------------------------------------------


async def test_an_off_grid_confidence_is_repaired_without_an_api_call(tmp_path):
    """No provider enforces `multipleOf`, so this is the common case. Spending a repair retry
    on it would swamp the prompt-health signal with quantisation noise."""
    payload = json.loads(json.dumps(VALID_CRITIQUE))
    payload["issues"][0]["confidence"] = 0.93
    fake = FakeProvider(script=[payload])
    outcome = await call(client_with(fake, tmp_path))
    assert outcome.repair_retries == 0  # no extra call
    assert outcome.local_repairs == ["issues[0].confidence 0.93 -> 0.95"]
    assert outcome.value.issues[0].confidence == 0.95
    assert len(fake.seen) == 1


async def test_local_repair_can_be_switched_off(tmp_path):
    payload = json.loads(json.dumps(VALID_CRITIQUE))
    payload["issues"][0]["confidence"] = 0.93
    fake = FakeProvider(script=[payload, VALID_CRITIQUE])
    outcome = await call(client_with(fake, tmp_path, repair_quantisation_locally=False))
    assert outcome.repair_retries == 1  # now it costs a call
    assert len(fake.seen) == 2


def test_quantisation_walks_a_payload_that_has_not_validated():
    """At repair time the payload may not have the shape the model expects, so the walker
    cannot rely on it."""
    data = {"issues": [{"confidence": 0.07}, {"confidence": 0.8}], "nested": {"confidence": 0.99}}
    repaired = _quantise_in_place(data)
    assert data["issues"][0]["confidence"] == 0.05
    assert data["issues"][1]["confidence"] == 0.8  # already on grid, untouched
    assert data["nested"]["confidence"] == 1.0
    assert len(repaired) == 2


def test_quantisation_ignores_a_boolean_confidence():
    """`True` is an int in Python. Snapping it to 1.0 would turn a type error into a silently
    accepted value."""
    data = {"confidence": True}
    assert _quantise_in_place(data) == []
    assert data["confidence"] is True


# -- the repair retry ------------------------------------------------------------------------


async def test_a_schema_violation_costs_exactly_one_repair_retry(tmp_path):
    broken = json.loads(json.dumps(VALID_CRITIQUE))
    broken["issues"][0]["severity"] = "low"  # verdict "block" needs a HIGH
    fake = FakeProvider(script=[broken, VALID_CRITIQUE])
    outcome = await call(client_with(fake, tmp_path))
    assert outcome.repair_retries == 1
    assert outcome.value.verdict == "block"


async def test_the_repair_prompt_names_the_validation_errors(tmp_path):
    broken = json.loads(json.dumps(VALID_CRITIQUE))
    broken["issues"][0]["evidence"] = []  # every issue MUST carry evidence
    fake = FakeProvider(script=[broken, VALID_CRITIQUE])
    await call(client_with(fake, tmp_path))
    retry = fake.seen[1].user
    assert "was rejected" in retry
    assert "evidence" in retry
    assert "usr" in retry  # the original request is preserved


async def test_exhausting_the_repair_budget_raises_rather_than_returning_junk(tmp_path):
    """The orchestrator records the critic as errored, which policy reads as `unassessed` --
    never as `clean`."""
    broken = json.loads(json.dumps(VALID_CRITIQUE))
    broken["verdict"] = "clean"  # clean with issues is incoherent
    fake = FakeProvider(script=[broken, broken])
    with pytest.raises(SchemaRepairFailed, match="could not produce a valid Critique"):
        await call(client_with(fake, tmp_path))


async def test_the_repair_budget_is_configurable(tmp_path):
    broken = json.loads(json.dumps(VALID_CRITIQUE))
    broken["verdict"] = "clean"
    fake = FakeProvider(script=[broken, broken, VALID_CRITIQUE])
    outcome = await call(client_with(fake, tmp_path, repair_retries=2))
    assert outcome.repair_retries == 2


# -- structure provenance --------------------------------------------------------------------


async def test_recovered_json_is_reported_as_extracted_not_as_native(tmp_path):
    """A provider claiming strict output that needed brace-scanning did not honour the
    guarantee, and the response must say so."""
    fake = FakeProvider(
        script=["<think>weighing severity</think>\n" + json.dumps(VALID_CRITIQUE)],
        structure=StructureMode.NATIVE_JSON,
    )
    outcome = await call(client_with(fake, tmp_path))
    assert outcome.response.structure is StructureMode.EXTRACTED
    assert not outcome.response.structure.is_guaranteed
    assert outcome.response.reasoning == "weighing severity"


async def test_only_strict_mode_counts_as_guaranteed():
    assert StructureMode.NATIVE_STRICT.is_guaranteed
    for mode in (StructureMode.NATIVE_SCHEMA, StructureMode.NATIVE_JSON, StructureMode.EXTRACTED):
        assert not mode.is_guaranteed


# -- transient failures ----------------------------------------------------------------------


async def test_a_transient_failure_is_retried_with_backoff(tmp_path, monkeypatch):
    """NVIDIA's shared hosted endpoint returns 503 ResourceExhausted under trivial
    concurrency, so this is measured behaviour, not a hypothetical."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake = FakeProvider(
        script=[ProviderTransientError("nim 503: ResourceExhausted 16/16"), VALID_CRITIQUE]
    )
    outcome = await call(client_with(fake, tmp_path))
    assert outcome.transient_retries == 1
    assert len(fake.seen) == 2


async def test_a_rate_limit_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake = FakeProvider(script=[ProviderRateLimited("429"), VALID_CRITIQUE])
    assert (await call(client_with(fake, tmp_path))).transient_retries == 1


async def test_a_non_retryable_failure_is_not_retried(tmp_path):
    fake = FakeProvider(script=[ProviderBadResponse("400 bad schema")])
    with pytest.raises(ProviderBadResponse):
        await call(client_with(fake, tmp_path))
    assert len(fake.seen) == 1


async def test_a_refusal_surfaces_as_a_refusal(tmp_path):
    """Expected traffic for a code reviewer: it is routinely asked to look at exploit-shaped
    code. It must be distinguishable from a transport error."""
    fake = FakeProvider(script=[ProviderRefused("declined (category=cyber)")])
    with pytest.raises(ProviderRefused):
        await call(client_with(fake, tmp_path))


async def test_retries_are_exhausted_then_raised(tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        providers=ProvidersConfig(
            nim=ProviderSettings(key_env="FAKE_KEY", max_retries=1)
        ),
    )
    client = LLMClient(settings)
    fake = FakeProvider(script=[ProviderTransientError("503"), ProviderTransientError("503")])
    client.providers[ProviderName.NIM] = fake
    with pytest.raises(ProviderTransientError):
        await call(client)
    assert len(fake.seen) == 2  # max_retries=1 means two attempts total


async def _no_sleep(_seconds: float) -> None:
    return None


# -- accounting ------------------------------------------------------------------------------


async def test_cost_and_tokens_accumulate_for_budget_enforcement(tmp_path):
    fake = FakeProvider(script=[VALID_CRITIQUE, VALID_CRITIQUE])
    client = client_with(fake, tmp_path)
    await call(client)
    await call(client, user="second")
    assert client.calls == 2
    assert client.total_cost_usd == pytest.approx(0.002)
    assert client.total_tokens == 300


async def test_a_replayed_call_is_free_and_uncounted(tmp_path):
    fake = FakeProvider(script=[VALID_CRITIQUE])
    recorder = client_with(fake, tmp_path, mode="record")
    await call(recorder)
    replayer = client_with(FakeProvider(script=[]), tmp_path, mode="replay")
    outcome = await call(replayer)
    assert outcome.response.replayed
    assert outcome.cost_usd == 0.0
    assert replayer.calls == 0


# -- cassettes -------------------------------------------------------------------------------


async def test_record_then_replay_round_trips(tmp_path):
    fake = FakeProvider(script=[VALID_CRITIQUE])
    recorded = await call(client_with(fake, tmp_path, mode="record"))
    replayed = await call(client_with(FakeProvider(script=[]), tmp_path, mode="replay"))
    assert replayed.value == recorded.value
    assert replayed.response.structure is recorded.response.structure


async def test_replay_fails_loudly_on_a_miss(tmp_path):
    """Falling through to the network is how "zero API calls in CI" quietly stops being true."""
    with pytest.raises(CassetteMiss, match="no cassette"):
        await call(client_with(FakeProvider(script=[]), tmp_path, mode="replay"))


def _request(**kw: Any) -> LLMRequest:
    defaults = dict(
        provider=ProviderName.NIM, model="m", system="s", user="u",
        json_schema={"type": "object"}, schema_name="Critique", max_tokens=100, effort="high",
    )
    return LLMRequest(**{**defaults, **kw})


def test_the_cassette_key_includes_the_provider():
    """A recording made against one provider must never serve another's request, or the suite
    claims coverage of a backend it never exercised."""
    assert request_hash(_request()) != request_hash(_request(provider=ProviderName.OPENAI))


def test_the_cassette_key_includes_the_adapted_schema():
    """Otherwise a dialect change silently reuses responses recorded against the old schema --
    invisible in exactly the tests meant to catch a schema regression."""
    other = {"type": "object", "additionalProperties": False}
    assert request_hash(_request()) != request_hash(_request(json_schema=other))


@pytest.mark.parametrize(
    "change",
    [{"model": "other"}, {"system": "other"}, {"user": "other"},
     {"effort": "low"}, {"max_tokens": 200}, {"schema_name": "PatchProposal"}],
)
def test_every_response_affecting_field_is_in_the_key(change):
    assert request_hash(_request()) != request_hash(_request(**change))


def test_the_key_is_stable_across_processes():
    """`json.dumps(sort_keys=True)` is load-bearing: without it the hash depends on dict
    insertion order, the same class of bug as a timestamp in a cached prompt prefix."""
    a = _request(json_schema={"b": 1, "a": 2})
    b = _request(json_schema={"a": 2, "b": 1})
    assert request_hash(a) == request_hash(b)


def test_cassettes_are_filed_per_provider(tmp_path):
    store = CassetteStore(tmp_path)
    assert store.path_for(_request()).parent.name == "nim"
    assert store.path_for(_request(provider=ProviderName.GEMINI)).parent.name == "gemini"


async def test_a_cassette_never_contains_credential_material(tmp_path):
    """Cassettes are committed to the repo. A key in one would be published."""
    fake = FakeProvider(script=[VALID_CRITIQUE])
    await call(client_with(fake, tmp_path, mode="record"))
    for path in tmp_path.rglob("*.json"):
        blob = path.read_text()
        assert "nvapi-" not in blob
        assert "sk-" not in blob


def test_counting_recordings_per_provider(tmp_path):
    store = CassetteStore(tmp_path)
    assert store.count() == {}
    (tmp_path / "nim").mkdir(parents=True)
    (tmp_path / "nim" / "a.json").write_text("{}")
    assert store.count() == {"nim": 1}


# -- extraction ------------------------------------------------------------------------------


def test_a_brace_inside_a_string_does_not_truncate_the_object():
    """An `Issue.explanation` mentioning a dict literal would unbalance naive brace counting."""
    text = '{"explanation": "the literal {\\"a\\": 1} is unsafe", "n": 2}'
    data, recovered, _ = parse_json_payload(text)
    assert data["n"] == 2
    assert not recovered


def test_reasoning_is_kept_rather_than_discarded():
    """It goes into the trace, so the viewer stays honest about why a critic said what it said
    even on providers with no reasoning summary of their own."""
    answer, reasoning = split_reasoning("<think>step one\nstep two</think>\n{\"a\": 1}")
    assert answer == '{"a": 1}'
    assert reasoning == "step one\nstep two"


def test_an_unclosed_think_tag_is_tolerated():
    """Happens when the response is cut off mid-thought by a token limit."""
    answer, reasoning = split_reasoning("<think>cut off here")
    assert answer == ""
    assert reasoning == "cut off here"


def test_a_truncated_object_is_diagnosed_as_a_token_limit():
    with pytest.raises(ProviderBadResponse, match="max_tokens"):
        parse_json_payload('{"verdict": "block", "issues": [{"id": "SEC-1"')


@pytest.mark.parametrize(
    "text", ["I cannot review this.", "[1, 2, 3]", "   ", ""]
)
def test_unusable_bodies_are_rejected(text):
    with pytest.raises(ProviderBadResponse):
        parse_json_payload(text)


def test_find_json_object_returns_none_when_there_is_none():
    assert find_json_object("no braces here") is None
