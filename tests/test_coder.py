"""The Coder: proposal validation, the PROPOSE<->VALIDATE cycle, and the six fixtures.

docs/03-agents.md § 3.1 sets the acceptance criterion: "6 fixture files with known bugs ->
assert the patch applies, the file parses, and the target line range is touched." That test
runs against committed cassettes, so it is reproducible without a credential; everything else
runs against a scripted fake provider.

The line-range assertion is the part that matters. "It applies and parses" would pass a patch
that rewrites an unrelated function, so the fixtures declare the range a correct fix has to
touch (`tests/fixtures/coder/manifest.py`).
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_agents import finding
from tests.test_llm_client import FakeProvider
from tribunal.agents import Coder, CoderBundle
from tribunal.agents.bundle import synthetic_issue
from tribunal.agents.validation import ProposalError, validate_patch_proposal
from tribunal.config import AgentConfig, AgentsConfig, LLMConfig, Settings
from tribunal.contracts import (
    Dimension,
    GroundingReport,
    PatchProposal,
    SearchReplaceEdit,
    UnaddressedIssue,
    finding_id,
)
from tribunal.grounding.suite import GroundingSuite
from tribunal.llm.base import ProviderName
from tribunal.llm.client import LLMClient, SchemaRepairFailed
from tribunal.patch import parse_unified_diff

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "coder"
CASSETTES = Path(__file__).parent / "cassettes"
sys.path.insert(0, str(FIXTURE_DIR))
from manifest import FIXTURES  # noqa: E402

SOURCE = '''import subprocess


def generate(name):
    subprocess.run("makereport " + name, shell=True)
    return name
'''

B602 = finding_id("bandit", "B602", "t.py", 5)
S602 = finding_id("ruff", "S602", "t.py", 5)


def report() -> GroundingReport:
    return GroundingReport(
        target="original", round=None,
        findings=[finding(B602, "bandit", "B602", 5), finding(S602, "ruff", "S602", 5)],
        measurements=[], tests=None, tool_errors={},
        tools_run=["astgate", "bandit", "radon", "ruff"],
    )


def bundle(**kw) -> CoderBundle:
    defaults = dict(
        round=1, filename="t.py", source=SOURCE, report=report(), max_hunks=8,
    )
    return CoderBundle(**{**defaults, **kw})


def proposal(**kw) -> PatchProposal:
    defaults = dict(
        round=1,
        edits=[
            SearchReplaceEdit(
                search='    subprocess.run("makereport " + name, shell=True)',
                replace='    subprocess.run(["makereport", name])',
            )
        ],
        diff="",
        rationale="drop shell=True and pass an argv list",
        addresses=[B602],
        deliberately_unaddressed=[],
    )
    return PatchProposal(**{**defaults, **kw})


def touched_lines(diff: str) -> set[int]:
    """1-based original-file lines a diff modifies or uses as context."""
    lines: set[int] = set()
    for hunk in parse_unified_diff(diff):
        cursor = hunk.old_start
        for _ in hunk.pre:
            lines.add(cursor)
            cursor += 1
    return lines


# -- the bundle ------------------------------------------------------------------------------


def test_the_source_is_line_numbered():
    assert "   5|     subprocess.run" in bundle().render()


def test_the_prompt_says_the_gutter_is_display_only():
    """The numbers are shown for orientation; copying them into an anchor is a common and
    otherwise baffling failure."""
    from tribunal.agents.prompts import load

    assert "never include them" in load("coder").text.lower()


def test_addressable_ids_are_findings_on_round_one():
    """No `Issue`s exist before the critics run, so finding ids are all there is."""
    assert bundle().addressable_ids() == {B602, S602}


def test_addressable_ids_include_open_issues_from_round_two():
    issue = synthetic_issue(Dimension.PERFORMANCE, "slow", "why", "t.py:L4-L6")
    assert issue.id in bundle(round=2, open_issues=(issue,)).addressable_ids()


def test_the_failing_test_is_framed_as_the_oracle():
    rendered = bundle(failing_test="def test_x():\n    assert True\n").render()
    assert "correctness oracle" in rendered
    assert "not a trade-off, it is broken" in rendered


def test_the_consolidated_critique_is_rendered_with_its_priority_order():
    rendered = bundle(
        round=2,
        consolidated_critique="1) drop shell=True",
        priority_order=("SEC-1", "PERF-2"),
    ).render()
    assert "single synthesised instruction set" in rendered
    assert "1. SEC-1" in rendered
    assert "2. PERF-2" in rendered


def test_dismissed_ids_tell_the_coder_not_to_satisfy_them():
    rendered = bundle(round=2, consolidated_critique="x", dismissed_issue_ids=("SEC-9",)).render()
    assert "do NOT change the code to satisfy" in rendered
    assert "SEC-9" in rendered


def test_a_validation_error_is_carried_into_the_next_attempt():
    """The retry is a re-anchoring exercise, not another guess at the fix."""
    retried = bundle().with_validation_error("hunk 2 failed to apply at line 47", "@@ -47 +47 @@")
    rendered = retried.render()
    assert "YOUR LAST ATTEMPT DID NOT APPLY" in rendered
    assert "hunk 2 failed to apply at line 47" in rendered
    assert "do not change your approach" in rendered


def test_earlier_rejected_diffs_are_shown_but_not_duplicated():
    """The most recent failure is in the retry block; older ones are listed separately, so the
    prompt does not show the same diff twice."""
    first = bundle().with_validation_error("anchor not found", "DIFF-A")
    second = first.with_validation_error("does not parse", "DIFF-B")
    rendered = second.render()
    assert rendered.count("DIFF-B") == 1
    assert "DIFF-A" in rendered
    assert "Do not propose these again" in rendered


def test_the_closing_lists_the_allowed_ids():
    rendered = bundle().render()
    assert B602 in rendered.split("Ids you may put in")[1]


# -- proposal validation ---------------------------------------------------------------------


def test_a_valid_proposal_passes():
    validate_patch_proposal(proposal(), bundle())


def test_an_invented_address_is_rejected():
    with pytest.raises(ProposalError, match="names ids that do not exist"):
        validate_patch_proposal(proposal(addresses=["SEC-made-up"]), bundle())


def test_an_invented_declined_id_is_rejected():
    bad = proposal(
        deliberately_unaddressed=[UnaddressedIssue(issue_id="nope", reason="false positive")]
    )
    with pytest.raises(ProposalError, match="names ids that do not exist"):
        validate_patch_proposal(bad, bundle())


def test_an_id_cannot_be_both_addressed_and_declined():
    bad = proposal(
        addresses=[B602],
        deliberately_unaddressed=[UnaddressedIssue(issue_id=B602, reason="actually fine")],
    )
    with pytest.raises(ProposalError, match="cannot be both"):
        validate_patch_proposal(bad, bundle())


def test_addressing_an_already_dismissed_id_is_rejected():
    """Contorting the code to satisfy a finding the Arbiter threw out is the exact pathology
    `deliberately_unaddressed` exists to prevent."""
    with pytest.raises(ProposalError, match="already dismissed"):
        validate_patch_proposal(proposal(), bundle(dismissed_issue_ids=(B602,)))


def test_the_wrong_round_is_rejected():
    with pytest.raises(ProposalError, match="round must be 2"):
        validate_patch_proposal(proposal(), bundle(round=2))


def test_an_empty_search_block_is_rejected():
    bad = proposal(edits=[SearchReplaceEdit(search="   \n", replace="x")])
    with pytest.raises(ProposalError, match="empty or whitespace"):
        validate_patch_proposal(bad, bundle())


def test_a_no_op_edit_is_rejected():
    same = '    subprocess.run("makereport " + name, shell=True)'
    bad = proposal(edits=[SearchReplaceEdit(search=same, replace=same)])
    with pytest.raises(ProposalError, match="no-op"):
        validate_patch_proposal(bad, bundle())


def test_a_copied_line_number_gutter_is_rejected():
    """Without this the anchor simply never matches and the error says only "not found"."""
    bad = proposal(
        edits=[
            SearchReplaceEdit(
                search='   5|     subprocess.run("makereport " + name, shell=True)',
                replace='   5|     subprocess.run(["makereport", name])',
            )
        ]
    )
    with pytest.raises(ProposalError, match="line-number gutter"):
        validate_patch_proposal(bad, bundle())


def test_overlapping_edits_are_rejected():
    """Observed live on the SQL-injection fixture: one edit covering two lines plus a second
    edit covering one of them. The first applied, the second matched rewritten text, and the
    result did not parse -- reported as "malformed Python", which sends the retry looking in
    entirely the wrong place."""
    bad = proposal(
        edits=[
            SearchReplaceEdit(
                search='    subprocess.run("makereport " + name, shell=True)\n    return name',
                replace='    subprocess.run(["makereport", name])\n    return name',
            ),
            SearchReplaceEdit(search="    return name", replace="    return str(name)"),
        ]
    )
    with pytest.raises(ProposalError, match="same lines of the source"):
        validate_patch_proposal(bad, bundle())


def test_non_overlapping_edits_are_accepted():
    ok = proposal(
        edits=[
            SearchReplaceEdit(
                search="import subprocess", replace="import shlex\nimport subprocess"
            ),
            SearchReplaceEdit(
                search='    subprocess.run("makereport " + name, shell=True)',
                replace='    subprocess.run(["makereport", name])',
            ),
        ]
    )
    validate_patch_proposal(ok, bundle())


def test_an_empty_proposal_is_legal_pushback():
    """"Nothing needs fixing" is a position the policy layer handles, not a malformed patch."""
    empty = proposal(
        edits=[],
        addresses=[],
        deliberately_unaddressed=[
            UnaddressedIssue(issue_id=B602, reason="name is a module constant")
        ],
    )
    validate_patch_proposal(empty, bundle())
    assert empty.is_empty


def test_all_problems_are_reported_at_once():
    bad = proposal(
        addresses=["invented"],
        edits=[SearchReplaceEdit(search="   ", replace="x")],
    )
    with pytest.raises(ProposalError) as caught:
        validate_patch_proposal(bad, bundle())
    message = str(caught.value)
    assert "do not exist" in message
    assert "empty or whitespace" in message


# -- the PROPOSE <-> VALIDATE cycle ----------------------------------------------------------


def coder_client(tmp_path: Path, script: list[Any]):
    settings = Settings(
        llm=LLMConfig(cassette_dir=tmp_path),
        agents=AgentsConfig(
            coder=AgentConfig(model="fake-1", provider=ProviderName.NIM, max_tokens=4000)
        ),
    )
    fake = FakeProvider(script=script)
    client = LLMClient(settings)
    client.providers[ProviderName.NIM] = fake
    return client, fake, settings


def payload(**kw) -> dict:
    base = json.loads(proposal().model_dump_json())
    base.update(kw)
    return base


async def test_a_good_patch_is_accepted_on_the_first_attempt(tmp_path):
    client, fake, settings = coder_client(tmp_path, [payload()])
    result = await Coder(settings, client).propose(bundle())
    assert result.ok
    assert result.patch_attempts == 1
    accepted = result.accepted
    assert accepted.validation.applied and accepted.validation.parse_ok
    assert 'subprocess.run(["makereport", name])' in accepted.patched_source
    assert len(fake.seen) == 1


async def test_a_bad_anchor_is_bounced_back_with_the_mechanical_error(tmp_path):
    bad = payload(edits=[{"search": "def nonexistent():", "replace": "x"}])
    client, fake, settings = coder_client(tmp_path, [bad, payload()])
    result = await Coder(settings, client).propose(bundle())
    assert result.ok
    assert result.patch_attempts == 2
    assert not result.attempts[0].ok
    retry_prompt = fake.seen[1].user
    assert "YOUR LAST ATTEMPT DID NOT APPLY" in retry_prompt
    assert "not found" in retry_prompt


async def test_validate_retries_do_not_consume_the_schema_repair_budget(tmp_path):
    """Three separate budgets, and conflating any two would hide a different problem."""
    bad = payload(edits=[{"search": "def nonexistent():", "replace": "x"}])
    client, fake, settings = coder_client(tmp_path, [bad, payload()])
    result = await Coder(settings, client).propose(bundle())
    assert result.patch_attempts == 2
    # Each attempt was schema-valid on its first try, so no repair retries were spent.
    assert all(a.run.outcome.repair_retries == 0 for a in result.attempts)


async def test_exhausting_the_patch_budget_yields_no_accepted_patch(tmp_path):
    bad = payload(edits=[{"search": "def nonexistent():", "replace": "x"}])
    client, fake, settings = coder_client(tmp_path, [bad, bad])
    result = await Coder(settings, client).propose(bundle())
    assert not result.ok
    assert result.accepted is None
    assert result.patch_attempts == 2
    assert "not found" in result.failure_reason


async def test_the_patch_budget_is_configurable(tmp_path):
    bad = payload(edits=[{"search": "def nonexistent():", "replace": "x"}])
    client, fake, settings = coder_client(tmp_path, [bad, bad, payload()])
    result = await Coder(settings, client).propose(bundle(), max_attempts=3)
    assert result.ok
    assert result.patch_attempts == 3


async def test_a_churn_heavy_patch_is_bounced(tmp_path):
    """Scope creep is mechanically visible in diff mode: ten extra hunks for one fix."""
    client, fake, settings = coder_client(tmp_path, [payload(), payload()])
    result = await Coder(settings, client).propose(bundle(max_hunks=0))
    assert not result.ok
    assert "over the limit of 0" in result.failure_reason
    # The work is preserved even though it was rejected.
    assert result.attempts[0].validation.patched_source is not None


async def test_a_patch_that_applies_but_does_not_parse_is_bounced(tmp_path):
    broken = payload(
        edits=[
            {
                "search": "    return name",
                "replace": "    return name(",
            }
        ]
    )
    client, fake, settings = coder_client(tmp_path, [broken, payload()])
    result = await Coder(settings, client).propose(bundle())
    assert result.ok
    first = result.attempts[0].validation
    assert first.applied and not first.parse_ok
    assert "does not parse" in first.failure_reason


async def test_an_incoherent_proposal_costs_a_schema_repair_not_a_patch_attempt(tmp_path):
    """Problems visible without applying the patch are repaired on the schema budget."""
    bad = payload(addresses=["invented-id"])
    client, fake, settings = coder_client(tmp_path, [bad, payload()])
    result = await Coder(settings, client).propose(bundle())
    assert result.ok
    assert result.patch_attempts == 1  # one VALIDATE cycle
    assert result.attempts[0].run.outcome.repair_retries == 1  # one schema repair


async def test_an_unrepairable_proposal_raises(tmp_path):
    bad = payload(addresses=["invented-id"])
    client, fake, settings = coder_client(tmp_path, [bad, bad])
    with pytest.raises(SchemaRepairFailed):
        await Coder(settings, client).propose(bundle())


async def test_an_empty_patch_passes_validate(tmp_path):
    """The Coder claiming nothing needs fixing must reach the policy layer, not die in
    VALIDATE."""
    client, fake, settings = coder_client(
        tmp_path, [payload(edits=[], addresses=[], rationale="nothing to fix here")]
    )
    result = await Coder(settings, client).propose(bundle())
    assert result.ok
    assert result.accepted.patch.diff == ""
    assert result.accepted.patched_source == SOURCE


async def test_the_trace_payload_separates_the_budgets(tmp_path):
    bad = payload(edits=[{"search": "def nonexistent():", "replace": "x"}])
    client, fake, settings = coder_client(tmp_path, [bad, payload()])
    result = await Coder(settings, client).propose(bundle())
    trace = result.trace_payload()
    assert trace["patch_attempts"] == 2
    assert trace["accepted"] is True
    assert len(trace["attempts"]) == 2
    assert trace["attempts"][0]["applied"] is False
    assert trace["attempts"][1]["parse_ok"] is True
    assert trace["attempts"][1]["addresses"] == [B602]
    assert all("diff_sha256" in a for a in trace["attempts"])


async def test_declined_findings_reach_the_trace(tmp_path):
    """The Arbiter adjudicates pushback explicitly, so it cannot be dropped here."""
    with_pushback = payload(
        addresses=[],
        deliberately_unaddressed=[{"issue_id": B602, "reason": "name is a module constant"}],
    )
    client, fake, settings = coder_client(tmp_path, [with_pushback])
    result = await Coder(settings, client).propose(bundle())
    assert result.trace_payload()["attempts"][0]["declined"] == [B602]


# -- the six fixtures, from committed cassettes ----------------------------------------------


#: The model these cassettes were recorded against, pinned here rather than taken from
#: `examples/nim.toml`. A replay test is about the recorded exchange, not about whatever the
#: example config routes to today -- and when the NIM default moved to the 49B model, every
#: lookup here missed and the whole suite went quietly to `skip`. docs/13 § 58.
CASSETTE_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def cassette_settings() -> Settings:
    settings = Settings.load(Path("examples/nim.toml"))
    replay = settings.llm.model_copy(update={"mode": "replay", "cassette_dir": CASSETTES})
    pinned = AgentConfig(
        model=CASSETTE_MODEL, provider=ProviderName.NIM, effort="high", max_tokens=8000
    )
    agents = settings.agents.model_copy(update={"coder": pinned})
    return settings.model_copy(update={"llm": replay, "agents": agents})


async def propose_for(fixture) -> tuple[str, Any]:
    settings = cassette_settings()
    source = (FIXTURE_DIR / fixture.filename).read_text(encoding="utf-8")
    grounded = await GroundingSuite(settings).run(source, logical_name=fixture.filename)
    issues = ()
    if fixture.needs_supplied_issue:
        low, high = fixture.target_lines
        issues = (
            synthetic_issue(
                Dimension.PERFORMANCE,
                fixture.summary,
                "Rebuilding the string each iteration is O(n^2).",
                f"{fixture.filename}:L{low}-L{high}",
            ),
        )
    bundle_ = CoderBundle(
        round=1, filename=fixture.filename, source=source, report=grounded.report,
        max_hunks=8, open_issues=issues,
    )
    return source, await Coder(settings).propose(bundle_, max_attempts=2)


def has_cassettes() -> bool:
    return CASSETTES.is_dir() and any(CASSETTES.rglob("*.json"))


requires_cassettes = pytest.mark.skipif(
    not has_cassettes(), reason="no cassettes recorded; run with a credential in record mode"
)


def test_there_are_six_fixtures_with_declared_target_ranges():
    """docs/03-agents.md § 3.1's acceptance criterion is six, not "some"."""
    assert len(FIXTURES) == 6
    for fixture in FIXTURES:
        assert (FIXTURE_DIR / fixture.filename).is_file()
        low, high = fixture.target_lines
        assert 1 <= low <= high
        total = len((FIXTURE_DIR / fixture.filename).read_text().splitlines())
        assert high <= total


def test_exactly_one_fixture_has_no_static_finding():
    """005 is kept deliberately: no ruff or bandit rule covers loop string concatenation, so it
    is the only fixture that drives the round >= 2 path where `addresses` names an issue id."""
    assert [f.filename for f in FIXTURES if f.needs_supplied_issue] == [
        "005-string-concat.py"
    ]


#: Fixtures that do not pass on the recorded backend, with the measured reason.
#:
#: `005-string-concat.py` is the one fixture whose defect sits inside a line dense with quote
#: characters (`text + ",".join(...) + "\n"`). NVIDIA Nemotron mangles those: the raw wire text
#: shows regex-style escaping and an opening double-quote substituted with `\(`. Prompt v4
#: ("`search` is literal text, not a pattern") fixed the *anchor* side -- the edit now applies,
#: where before it could not be found -- but the *replacement* still arrives with an
#: unterminated string literal.
#:
#: Left as a non-strict xfail rather than deleted, for the same reason as the sandbox network
#: row: a suite that documents its own limitation is better than one that looks complete. It is
#: also precisely what `StructureMode` exists to attribute -- NIM is `native_json`, with no
#: schema-constrained decoding, and this is the class of defect that separates it from a
#: `native_strict` backend. Non-strict, so it reports XPASS rather than failing if a future
#: prompt or model clears it.
KNOWN_PROVIDER_LIMITATIONS = {
    "005-string-concat.py": (
        "NVIDIA Nemotron mangles quote characters in a replacement dense with them: the "
        "patched source comes back with an unterminated string literal. Model-side "
        "serialisation, visible in the raw wire text, not recoverable by de-escaping."
    ),
}


def _fixture_param(fixture):
    reason = KNOWN_PROVIDER_LIMITATIONS.get(fixture.filename)
    marks = [pytest.mark.xfail(reason=reason, strict=False)] if reason else []
    return pytest.param(fixture, marks=marks, id=fixture.filename)


@requires_cassettes
@pytest.mark.parametrize("fixture", [_fixture_param(f) for f in FIXTURES])
def test_the_patch_applies_parses_and_touches_the_target(fixture):
    """The Coder's acceptance criterion, verbatim from docs/03-agents.md § 3.1.

    All three clauses are asserted. "Applies and parses" alone would pass a patch that
    rewrote an unrelated function, which is why the fixtures declare a target range.
    """
    # A miss is a FAILURE, not a skip. The model is pinned above, so the only things that
    # can cause one are a prompt or schema change -- exactly what this suite exists to
    # notice. As a skip it let the Coder's acceptance criterion stop being tested without
    # a single red mark (docs/13 § 58).
    source, result = asyncio.run(propose_for(fixture))

    assert result.ok, (
        f"{fixture.filename}: no patch applied in {result.patch_attempts} attempt(s). "
        f"Last error: {result.failure_reason}"
    )
    accepted = result.accepted
    assert accepted.validation.applied
    assert accepted.validation.parse_ok
    ast.parse(accepted.patched_source, filename=fixture.filename)

    low, high = fixture.target_lines
    touched = touched_lines(accepted.patch.diff)
    assert touched & set(range(low, high + 1)), (
        f"{fixture.filename}: patch touched lines {sorted(touched)}, but the defect is at "
        f"L{low}-L{high}. It applied and parsed without fixing the bug."
    )
    assert accepted.validation.hunks <= 8
    assert accepted.patched_source != source


def test_the_known_limitations_list_is_not_a_dumping_ground():
    """One documented failure is a limitation; a growing list is a broken agent.

    Also guards against a stale entry: every name here must still be a real fixture.
    """
    names = {f.filename for f in FIXTURES}
    assert set(KNOWN_PROVIDER_LIMITATIONS) <= names
    assert len(KNOWN_PROVIDER_LIMITATIONS) <= 1, (
        "more than one fixture is failing on the recorded backend; that is a Coder problem, "
        "not a provider limitation"
    )


def test_the_pinned_cassette_model_is_one_the_cassettes_were_recorded_against():
    """Protects the fix for docs/13 § 58 rather than the bug.

    The suite pins `CASSETTE_MODEL` instead of following `examples/nim.toml`. If someone
    re-records against a different model, or edits the pin, every lookup misses again — and
    the symptom last time was six tests quietly turning into skips while the run still said
    "passed". This makes that a named failure instead.
    """
    recorded = {
        json.loads(path.read_text(encoding="utf-8"))["request"]["model"]
        for path in CASSETTES.rglob("*.json")
    }
    assert CASSETTE_MODEL in recorded, (
        f"the suite pins {CASSETTE_MODEL!r}, but the committed cassettes were recorded "
        f"against {sorted(recorded)}. Nothing would match, and the whole Coder acceptance "
        "criterion would skip rather than fail."
    )


def test_the_fixture_suite_is_not_silently_skipping():
    """The cassettes exist, so the fixture tests must actually run.

    `requires_cassettes` is the legitimate "a fresh clone with no recordings" skip, and it
    is loud because it takes the whole suite with it. What is not legitimate is the suite
    appearing to run while every case inside it skips.
    """
    assert has_cassettes()
    for fixture in FIXTURES:
        matching = [
            path for path in CASSETTES.rglob("*.json")
            if fixture.filename.removesuffix(".py").split("-", 1)[1]
            in path.read_text(encoding="utf-8")
        ]
        assert matching, f"no cassette mentions {fixture.filename}"
