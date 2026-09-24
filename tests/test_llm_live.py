"""Recorded-cassette replay (always runs) and live-API tests (only with a credential).

The replay half is the important one: it proves the committed recordings still satisfy the
current contract. If a schema change breaks a cassette, that is a real regression -- the
recorded response is what a provider actually said, and if it no longer validates then either
the contract moved or the prompt needs to move with it.

The live half is marked `live` and skipped unless the relevant key is set, so CI stays at zero
API calls. Run one explicitly with:

    NVIDIA_API_KEY=... pytest -m live -k nim
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tribunal.agents import Arbiter, Postmortem
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    ArbiterNote,
    ConflictAffirmation,
    Critique,
    Decision,
    PatchProposal,
    PostmortemNote,
)
from tribunal.llm.base import ProviderName, StructureMode
from tribunal.llm.client import LLMClient
from tribunal.llm.registry import build

CASSETTES = Path(__file__).parent / "cassettes"


def committed_cassettes() -> list[Path]:
    if not CASSETTES.is_dir():
        return []
    return sorted(CASSETTES.rglob("*.json"))


CASSETTE_FILES = committed_cassettes()
cassette_case = pytest.mark.parametrize(
    "path", CASSETTE_FILES, ids=[f"{p.parent.name}/{p.stem[:8]}" for p in CASSETTE_FILES]
)


# -- replay ----------------------------------------------------------------------------------


#: Every output model a cassette may carry. A cassette whose schema is not here would be
#: skipped silently, so the set is asserted to cover what is on disk.
REPLAYABLE = {
    "Critique": Critique,
    "PatchProposal": PatchProposal,
    "ArbiterNote": ArbiterNote,
    "ConflictAffirmation": ConflictAffirmation,
    "PostmortemNote": PostmortemNote,
}


@pytest.mark.skipif(not CASSETTE_FILES, reason="no cassettes committed yet")
@cassette_case
def test_a_recorded_response_still_satisfies_the_contract(path):
    """A cassette that stops validating is a contract regression, not a stale fixture.

    The recorded response is what a provider actually said. If it no longer validates, either
    the contract moved or the prompt needs to move with it -- both are things to know.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema_name = payload["request"]["schema_name"]
    model = REPLAYABLE[schema_name]
    value = model.model_validate(payload["response"]["data"])
    if isinstance(value, Critique):
        assert value.tools_consulted
        for issue in value.issues:
            assert issue.evidence
    elif isinstance(value, PatchProposal):
        # A proposal must pick one format, and an empty one is legal pushback.
        assert not (value.edits and value.diff.strip())
    elif isinstance(value, ArbiterNote):
        # The branch rule is a schema validator, so reaching here means it held. What the
        # schema cannot see is the ordering invariant: a dismissal is not also a priority.
        assert not ({e.issue_id for e in value.dismissed} & set(value.priority_order))
    elif isinstance(value, PostmortemNote):
        # Non-empty is the schema's rule; that it stays non-empty across a model change
        # is what a recording proves.
        assert value.what_i_would_not_trust
    else:
        assert value.left_issue != value.right_issue


@pytest.mark.skipif(not CASSETTE_FILES, reason="no cassettes committed yet")
@cassette_case
def test_every_cassette_schema_has_a_validator(path):
    """Guards against a new agent's cassettes being silently skipped by the tests above."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["request"]["schema_name"] in REPLAYABLE


@pytest.mark.skipif(not CASSETTE_FILES, reason="no cassettes committed yet")
@cassette_case
def test_a_cassette_replays_through_the_client_with_no_credential(path, monkeypatch):
    """The end-to-end replay path, driven by the request the recording was made with."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    request = payload["request"]
    for provider in ProviderName:
        monkeypatch.delenv(build(provider).key_env, raising=False)

    client = LLMClient(Settings(llm=LLMConfig(mode="replay", cassette_dir=CASSETTES)))
    outcome = asyncio.run(
        client.call(
            provider=ProviderName(request["provider"]),
            model=request["model"],
            system=request["system"],
            user=request["user"],
            output_model=REPLAYABLE[request["schema_name"]],
            max_tokens=request["max_tokens"],
            effort=request["effort"],
        )
    )
    assert outcome.response.replayed
    assert outcome.request_hash == payload["hash"]
    assert client.calls == 0
    assert outcome.cost_usd == 0.0


@pytest.mark.skipif(not CASSETTE_FILES, reason="no cassettes committed yet")
@cassette_case
def test_a_cassette_carries_no_credential_material(path):
    """Cassettes are committed, so anything in one is published."""
    blob = path.read_text(encoding="utf-8")
    for marker in ("nvapi-", "sk-ant-", "sk-proj-", "AIza"):
        assert marker not in blob, f"{path.name} contains {marker}"


@pytest.mark.skipif(not CASSETTE_FILES, reason="no cassettes committed yet")
@cassette_case
def test_a_cassette_records_how_the_structure_was_obtained(path):
    """`native_json` and `extracted` are the difference between "the provider honoured the
    schema" and "we recovered the JSON ourselves". A recording without it is not interpretable
    later."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert StructureMode(payload["response"]["structure"])


def test_the_cassette_directory_is_organised_by_provider():
    for path in CASSETTE_FILES:
        assert path.parent.name in {p.value for p in ProviderName}


# -- live ------------------------------------------------------------------------------------

REDTEAM_SYSTEM = """You are the Red-team critic in an adversarial code review system. You \
assess ONE dimension: security. Emit a single JSON object matching the Critique schema.

Hard rules:
- Every issue MUST carry at least one evidence entry, kind="tool_finding" with ref set to the
  grounding finding id it cites.
- confidence must be a multiple of 0.05.
- verdict "block" requires at least one issue with severity "high"; "clean" requires an empty
  issues list. Finding nothing is a legitimate and expected outcome on clean code.
- tools_consulted lists the grounding TOOL NAMES you used (e.g. "bandit", "ruff"), never
  finding ids.
- One issue per distinct defect. If two tools report the same defect at the same line, that is
  ONE issue citing both findings as evidence.
- Re-rate severity in context; do not copy the tool's rating."""

VULNERABLE_USER = """Round 1. Assess the patched file for security issues.

patched.py:
  1| import subprocess
  2|
  3| def run_report(name):
  4|     subprocess.run("generate " + name, shell=True)

Grounding findings on patched.py:
  - id=e5376668fa tool=bandit rule=B602 severity=HIGH line=4
    "subprocess call with shell=True identified, security issue."
  - id=512b792e4f tool=ruff rule=S602 severity=error line=4
    "`subprocess` call with `shell=True` identified, security issue"

Emit the Critique with dimension="security" and round=1."""

CLEAN_USER = """Round 1. Assess the patched file for security issues.

patched.py:
  1| def normalise(name: str) -> str:
  2|     return name.strip().lower()

Grounding findings on patched.py:
  (none)

Emit the Critique with dimension="security" and round=1."""


#: Two genuine defects sharing a line that do not exclude each other: the injection and the
#: quadratic concatenation are fixed independently. The non-example detector 2 must not
#: affirm.
UNOPPOSED_SOURCE = '''import subprocess


def collect(names):
    out = []
    for name in names:
        out = out + [subprocess.check_output("stat " + name, shell=True)]
    return out
'''


def _live_settings() -> Settings:
    """Every Arbiter seat on the one provider a live run is cheap on."""
    def agent(effort: str) -> AgentConfig:
        return AgentConfig(
            model=build(ProviderName.NIM).default_model,
            provider=ProviderName.NIM,
            effort=effort,
            max_tokens=8000,
        )

    return Settings(
        llm=LLMConfig(mode="live"),
        agents=AgentsConfig(
            arbiter=agent("high"),
            arbiter_affirm=agent("medium"),
            postmortem=agent("medium"),
        ),
    )


def _live_or_skip(provider: ProviderName):
    prov = build(provider)
    reason = prov.available()
    if reason:
        pytest.skip(f"{provider.value}: {reason}")
    return prov


@pytest.mark.live
@pytest.mark.parametrize("provider", list(ProviderName), ids=lambda p: p.value)
def test_live_critic_produces_a_valid_critique(provider):
    """One round-trip per configured provider. Skipped without a key."""
    _live_or_skip(provider)
    settings = Settings(llm=LLMConfig(mode="live"))
    client = LLMClient(settings)
    prov = client.providers[provider]

    outcome = asyncio.run(
        client.call(
            provider=provider,
            model=prov.default_model,
            system=REDTEAM_SYSTEM,
            user=VULNERABLE_USER,
            output_model=Critique,
            max_tokens=8000,
            effort="medium",
        )
    )
    critique = outcome.value
    assert critique.verdict in ("block", "concerns")
    assert critique.issues
    # Grounded, not invented: every issue must cite one of the ids we supplied.
    supplied = {"e5376668fa", "512b792e4f"}
    cited = {e.ref for issue in critique.issues for e in issue.evidence}
    assert cited & supplied, f"no issue cited a supplied finding id: {cited}"
    # tools_consulted must be tool names, not finding ids -- the model gets this wrong
    # without the explicit instruction, so it is worth asserting.
    assert not (set(critique.tools_consulted) & supplied)


@pytest.mark.live
@pytest.mark.parametrize("provider", list(ProviderName), ids=lambda p: p.value)
def test_live_critic_does_not_manufacture_issues_on_clean_code(provider):
    """The canary case, against a live model. docs/11-risks.md R2: a critic told to find issues
    will find issues. `clean` has to be reachable or the whole debate is theatre."""
    _live_or_skip(provider)
    client = LLMClient(Settings(llm=LLMConfig(mode="live")))
    prov = client.providers[provider]

    outcome = asyncio.run(
        client.call(
            provider=provider,
            model=prov.default_model,
            system=REDTEAM_SYSTEM,
            user=CLEAN_USER,
            output_model=Critique,
            max_tokens=8000,
            effort="medium",
        )
    )
    critique = outcome.value
    # Not a hard assertion on `clean`: a HIGH here would be inflation, but a LOW note is
    # defensible. What must hold is that nothing is both severe and ungrounded.
    for issue in critique.issues:
        assert not (issue.severity.value == "high" and not issue.grounded), (
            f"manufactured a high-severity ungrounded issue on clean code: {issue.title}"
        )


@pytest.mark.live
def test_live_nim_reports_reasoning_separately_from_the_answer():
    """Pins the measured behaviour that contradicts NVIDIA's prose docs: the hosted endpoint
    returns reasoning in `reasoning_content`, with clean JSON in `content`."""
    _live_or_skip(ProviderName.NIM)
    client = LLMClient(Settings(llm=LLMConfig(mode="live")))
    outcome = asyncio.run(
        client.call(
            provider=ProviderName.NIM,
            model=build(ProviderName.NIM).default_model,
            system=REDTEAM_SYSTEM,
            user=VULNERABLE_USER,
            output_model=Critique,
            max_tokens=8000,
            effort="medium",
        )
    )
    assert outcome.response.structure is StructureMode.NATIVE_JSON
    assert "<think>" not in outcome.response.raw_text
    assert outcome.response.reasoning


@pytest.mark.live
def test_live_arbiter_writes_up_a_decision_it_cannot_change():
    """The real prompt, the real bundle, a real model -- and the check that matters.

    Run against the agent rather than a hand-written system string, because the thing under
    test is whether *this prompt* keeps a capable model from arguing with the verdict. A
    condensed paraphrase would test a prompt nobody ships.
    """
    _live_or_skip(ProviderName.NIM)
    from tests.test_arbiter import bundle

    settings = _live_settings()
    run = asyncio.run(Arbiter(settings).run(bundle()))
    note = run.value
    assert note.decision_echo is Decision.REJECT
    assert note.consolidated_critique
    # It was given no pushback to adjudicate, so it has nothing it may dismiss.
    assert note.dismissed == []


@pytest.mark.live
def test_live_arbiter_declines_to_affirm_an_unopposed_pair():
    """docs/11-risks.md R4, against a live model: the failure mode is a model that says yes.

    Two real defects on one line that do not exclude each other. An affirmation here would
    ship a mediocre patch with a confident trade-off justification attached, which is the
    worst output the system can produce.
    """
    _live_or_skip(ProviderName.NIM)
    from tests.test_arbiter import unopposed_pair

    settings = _live_settings()
    left, right = unopposed_pair()
    run = asyncio.run(
        Arbiter(settings).affirms(left, right, 1, "target.py", UNOPPOSED_SOURCE)
    )
    assert run.value.opposing is False, run.value.reasoning
    assert (run.value.left_issue, run.value.right_issue) == (left.id, right.id)


@pytest.mark.live
def test_live_postmortem_never_hides_an_open_issue():
    """The one thing a write-up must not do, against a live model.

    An accepted patch with an open issue is a conditional pass. A narrative that reads as an
    unconditional one is worse than no narrative: it teaches the reader to stop looking, and
    it is exactly what a fluent summariser produces when the outcome says "accept".
    """
    _live_or_skip(ProviderName.NIM)
    from tests.test_postmortem import SEC, bundle, issue

    settings = _live_settings()
    conditional = bundle(open_issues=(issue(),), fixed_issues=())
    run = asyncio.run(Postmortem(settings).run(conditional, 1))
    assert run.value.outcome_echo is Decision.ACCEPT
    assert any(SEC in caveat for caveat in run.value.what_i_would_not_trust), (
        f"the open issue was not disclosed: {run.value.what_i_would_not_trust}"
    )
