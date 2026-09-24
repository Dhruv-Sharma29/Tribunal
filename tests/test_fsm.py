"""The state machine.

Two things are being proved here.

**The table is total.** Every `(state, trigger)` pair either has an entry or raises
`IllegalTransition` naming both. A chain of `elif`s cannot give you that, which is why
docs/09-roadmap.md Phase 0 asks for an explicit transition table.

**Criterion S1: a run terminates in a defined state, always.** docs/04-arbitration.md makes
this a property test over randomly generated critique sequences: every run reaches a terminal
state within `max_rounds x patch_attempts + 2` proposals. That test drives the *real* policy
layer, not a stub, so it is exercising the loop the orchestrator will run.
"""

from __future__ import annotations

import random

import pytest

from tribunal.config import BudgetConfig, PolicyConfig
from tribunal.contracts import (
    Critique,
    Decision,
    Dimension,
    Evidence,
    EvidenceKind,
    Issue,
    Severity,
)
from tribunal.fsm import (
    DECISION_TRIGGERS,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    Machine,
    State,
    Trigger,
    legal_triggers,
    next_state,
    reachable_states,
    trigger_for_decision,
)
from tribunal.policy import PolicyInput, Spend, decide

HAPPY_PATH = (
    Trigger.READY,
    Trigger.GROUNDED,
    Trigger.PROPOSED,
    Trigger.PATCH_OK,
    Trigger.CRITIQUED,
    Trigger.ACCEPT,
    Trigger.WRITTEN,
)


# -- the table -------------------------------------------------------------------------------


def test_the_table_matches_the_architecture_diagram():
    """Diffable by eye against docs/01-architecture.md § The state machine."""
    assert TRANSITIONS == {
        (State.INIT, Trigger.READY): State.GROUND,
        (State.GROUND, Trigger.GROUNDED): State.PROPOSE,
        (State.GROUND, Trigger.INPUT_UNUSABLE): State.FAILED,
        (State.PROPOSE, Trigger.PROPOSED): State.VALIDATE,
        (State.VALIDATE, Trigger.PATCH_REJECTED_RETRIES_LEFT): State.PROPOSE,
        (State.VALIDATE, Trigger.PATCH_REJECTED_EXHAUSTED): State.FAILED,
        (State.VALIDATE, Trigger.PATCH_OK): State.CRITIQUE,
        (State.CRITIQUE, Trigger.CRITIQUED): State.ARBITRATE,
        (State.ARBITRATE, Trigger.REJECT): State.PROPOSE,
        (State.ARBITRATE, Trigger.ESCALATE): State.ESCALATE,
        (State.ARBITRATE, Trigger.TRADEOFF): State.TRADEOFF,
        (State.ARBITRATE, Trigger.ACCEPT): State.POSTMORTEM,
        (State.TRADEOFF, Trigger.RECORDED): State.POSTMORTEM,
        (State.ESCALATE, Trigger.RECORDED): State.POSTMORTEM,
        (State.POSTMORTEM, Trigger.WRITTEN): State.DONE,
    }


def test_every_state_is_reachable_from_init():
    """An orphaned state is dead code that looks like design."""
    assert reachable_states() == set(State)


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL:
        assert legal_triggers(state) == ()


def test_both_non_accept_terminals_still_write_a_postmortem():
    """An escalated run must produce a report and the best patch so far, not lose the work."""
    assert TRANSITIONS[(State.ESCALATE, Trigger.RECORDED)] is State.POSTMORTEM
    assert TRANSITIONS[(State.TRADEOFF, Trigger.RECORDED)] is State.POSTMORTEM


@pytest.mark.parametrize("state", list(State), ids=lambda s: s.value)
@pytest.mark.parametrize("trigger", list(Trigger), ids=lambda t: t.value)
def test_every_pair_is_either_defined_or_raises(state, trigger):
    """The exhaustive guard. No pair silently no-ops, which is how a run ends in an undefined
    state — exactly what charter criterion S1 forbids."""
    if (state, trigger) in TRANSITIONS:
        assert next_state(state, trigger) in State
    else:
        with pytest.raises(IllegalTransition):
            next_state(state, trigger)


def test_the_illegal_transition_error_names_the_legal_alternatives():
    with pytest.raises(IllegalTransition) as caught:
        next_state(State.GROUND, Trigger.ACCEPT)
    message = str(caught.value)
    assert "GROUND" in message and "accept" in message
    assert "grounded" in message  # the trigger that would have worked


def test_a_terminal_state_says_it_is_terminal_in_the_error():
    with pytest.raises(IllegalTransition, match="terminal"):
        next_state(State.DONE, Trigger.READY)


def test_every_decision_has_a_trigger():
    """A new decision cannot be added without a transition for it."""
    assert set(DECISION_TRIGGERS) == set(Decision)
    for trigger in DECISION_TRIGGERS.values():
        assert (State.ARBITRATE, trigger) in TRANSITIONS


# -- walking the machine ---------------------------------------------------------------------


def test_the_happy_path_reaches_done():
    machine = Machine()
    for trigger in HAPPY_PATH:
        machine = machine.step(trigger)
    assert machine.state is State.DONE
    assert machine.is_terminal
    assert machine.history[0] is State.INIT
    assert machine.round == 1


def test_a_machine_is_immutable():
    """A run is a list of values, which is what lets a trace replay into the same states."""
    start = Machine()
    moved = start.step(Trigger.READY)
    assert start.state is State.INIT
    assert moved.state is State.GROUND
    assert moved is not start


def test_a_validate_bounce_does_not_start_a_new_round():
    """Re-anchoring inside a round is a patch attempt, not a debate round — the same
    distinction docs/03-agents.md draws between the two budgets."""
    machine = Machine()
    for trigger in (Trigger.READY, Trigger.GROUNDED, Trigger.PROPOSED):
        machine = machine.step(trigger)
    assert (machine.round, machine.patch_attempts) == (1, 0)

    machine = machine.step(Trigger.PATCH_REJECTED_RETRIES_LEFT)
    assert machine.state is State.PROPOSE
    assert (machine.round, machine.patch_attempts) == (1, 1)


def test_an_arbitrate_reject_starts_a_new_round_and_resets_attempts():
    machine = Machine()
    for trigger in (Trigger.READY, Trigger.GROUNDED, Trigger.PROPOSED,
                    Trigger.PATCH_REJECTED_RETRIES_LEFT, Trigger.PROPOSED,
                    Trigger.PATCH_OK, Trigger.CRITIQUED):
        machine = machine.step(trigger)
    assert (machine.round, machine.patch_attempts) == (1, 1)

    machine = machine.step(Trigger.REJECT)
    assert machine.state is State.PROPOSE
    assert (machine.round, machine.patch_attempts) == (2, 0)


def test_exhausting_patch_attempts_fails_without_calling_a_critic():
    """VALIDATE is the cheap gate: a malformed patch never reaches either critic."""
    machine = Machine()
    for trigger in (Trigger.READY, Trigger.GROUNDED, Trigger.PROPOSED,
                    Trigger.PATCH_REJECTED_EXHAUSTED):
        machine = machine.step(trigger)
    assert machine.state is State.FAILED
    assert State.CRITIQUE not in machine.history


def test_a_reject_on_the_final_round_escalates_instead_of_looping():
    """Policy's `rounds_exhausted` handles the high-pressure case, but a REJECT from another
    row — a correctness regression, say — must not loop forever."""
    assert trigger_for_decision(Decision.REJECT, rounds_remain=True) is Trigger.REJECT
    assert trigger_for_decision(Decision.REJECT, rounds_remain=False) is Trigger.ESCALATE


@pytest.mark.parametrize(
    "decision", [Decision.ACCEPT, Decision.TRADEOFF, Decision.ESCALATE], ids=lambda d: d.value
)
def test_non_reject_decisions_are_unaffected_by_the_round_guard(decision):
    assert trigger_for_decision(decision, rounds_remain=False) is DECISION_TRIGGERS[decision]


# -- the property test: criterion S1 -----------------------------------------------------------


def _issue(rng: random.Random, dimension: Dimension, index: int) -> Issue:
    severity = rng.choice(list(Severity))
    kind = rng.choice([EvidenceKind.TOOL_FINDING, EvidenceKind.CODE_SPAN, EvidenceKind.REASONING])
    prefix = "SEC" if dimension is Dimension.SECURITY else "PERF"
    return Issue(
        id=f"{prefix}-{index}",
        dimension=dimension,
        severity=severity,
        title="t",
        explanation="e",
        evidence=[Evidence(kind=kind, ref=f"ref-{prefix}-{index}", excerpt="")],
        confidence=rng.choice([0.0, 0.5, 0.6, 0.85, 1.0]),
        introduced_by_patch=rng.choice([True, False]),
        suggested_direction=None,
    )


def _critique(rng: random.Random, dimension: Dimension, round_: int) -> Critique:
    issues = [_issue(rng, dimension, n) for n in range(rng.randint(0, 3))]
    if not issues:
        verdict = "clean"
    elif any(i.severity is Severity.HIGH for i in issues):
        verdict = rng.choice(["block", "block"])
    else:
        verdict = "concerns"
    return Critique(
        dimension=dimension,
        round=round_,
        verdict=verdict,
        issues=issues,
        tools_consulted=["bandit"],
        summary="s",
    )


def _run(rng: random.Random, config: PolicyConfig, budget: BudgetConfig) -> Machine:
    """Drive the real FSM with the real policy over randomly generated critiques."""
    machine = Machine().step(Trigger.READY).step(Trigger.GROUNDED)
    history: list[float] = []
    hashes: list[str] = []
    guard = 0

    while not machine.is_terminal:
        guard += 1
        if guard > 200:  # pragma: no cover - the assertion below is the real bound
            raise AssertionError(f"run did not terminate: {machine.history}")

        machine = machine.step(Trigger.PROPOSED)

        if rng.random() < 0.25:
            attempts_left = machine.patch_attempts + 1 < config.patch_attempts_per_round
            machine = machine.step(
                Trigger.PATCH_REJECTED_RETRIES_LEFT
                if attempts_left
                else Trigger.PATCH_REJECTED_EXHAUSTED
            )
            continue

        machine = machine.step(Trigger.PATCH_OK).step(Trigger.CRITIQUED)

        critiques = tuple(
            _critique(rng, dimension, machine.round)
            for dimension in (Dimension.SECURITY, Dimension.PERFORMANCE)
            if rng.random() > 0.15  # sometimes a critic is missing entirely
        )
        errored = tuple(
            d
            for d in (Dimension.SECURITY, Dimension.PERFORMANCE)
            if d not in {c.dimension for c in critiques}
        )
        diff_hash = rng.choice(["a", "b", "c"])
        verdict = decide(
            PolicyInput(
                round=machine.round,
                critiques=critiques,
                errored_dimensions=errored,
                diff_hash=diff_hash,
                previous_diff_hashes=tuple(hashes),
                pressure_history=tuple(history),
                spend=Spend(usd=rng.uniform(0, 3), rounds=machine.round),
            ),
            config,
            budget,
        )
        history.append(verdict.pressure)
        hashes.append(diff_hash)

        machine = machine.step(
            trigger_for_decision(verdict.decision, rounds_remain=machine.round < config.max_rounds)
        )
        if machine.state in (State.ESCALATE, State.TRADEOFF):
            machine = machine.step(Trigger.RECORDED)
        if machine.state is State.POSTMORTEM:
            machine = machine.step(Trigger.WRITTEN)

    return machine


@pytest.mark.parametrize("seed", range(300))
def test_every_random_run_terminates_within_the_bound(seed):
    """Charter criterion S1, and the reason `policy` and `fsm` are pure.

    The bound is `max_rounds x patch_attempts + 2` **proposals**, not state edges: one debate
    round can legitimately contain several PROPOSE entries, one per re-anchoring attempt.
    """
    config = PolicyConfig()
    machine = _run(random.Random(seed), config, BudgetConfig())

    assert machine.is_terminal
    assert machine.state in TERMINAL
    proposals = sum(1 for state in machine.history if state is State.PROPOSE)
    bound = config.max_rounds * config.patch_attempts_per_round + 2
    assert proposals <= bound, f"{proposals} proposals exceeds the bound of {bound}"


@pytest.mark.parametrize("seed", range(60))
def test_a_terminated_run_always_ends_done_or_failed(seed):
    machine = _run(random.Random(seed), PolicyConfig(), BudgetConfig())
    assert machine.state in (State.DONE, State.FAILED)
    # FAILED is only reachable from VALIDATE, so a run that reached a critic cannot fail there.
    if machine.state is State.FAILED:
        assert machine.history[-2] is State.VALIDATE


@pytest.mark.parametrize("seed", range(40))
def test_runs_terminate_under_a_tighter_round_budget_too(seed):
    config = PolicyConfig(max_rounds=1, patch_attempts_per_round=1)
    machine = _run(random.Random(seed), config, BudgetConfig())
    assert machine.is_terminal
    proposals = sum(1 for state in machine.history if state is State.PROPOSE)
    assert proposals <= config.max_rounds * config.patch_attempts_per_round + 2


def test_the_history_of_every_run_is_a_legal_walk():
    """Belt and braces: replay each recorded history against the table."""
    for seed in range(40):
        machine = _run(random.Random(seed), PolicyConfig(), BudgetConfig())
        edges = {(source, target) for (source, _), target in TRANSITIONS.items()}
        for source, target in zip(machine.history, machine.history[1:], strict=False):
            assert (source, target) in edges, f"{source} -> {target} is not in the table"
