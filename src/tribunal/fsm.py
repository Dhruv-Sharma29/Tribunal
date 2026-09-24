"""The state machine. Pure: states, transitions and guards, no I/O.

docs/01-architecture.md § Repo layout makes the reason explicit -- `fsm.py` and `policy.py`
take values and return values, which is what makes the machine exhaustively unit-testable
without an API key and what makes replay possible.

The transitions are a **table**, not nested `if`s (docs/09-roadmap.md Phase 0: "FSMs as
explicit transition tables"). Two things fall out of that which a chain of conditionals does
not give you:

* An undefined `(state, trigger)` pair raises `IllegalTransition` naming both, instead of
  silently falling through to whatever the last `elif` was. Charter criterion S1 -- "a run
  terminates in a defined state, always" -- is a property of the table being total, and
  `tests/test_fsm.py` walks every pair to prove it.
* The diagram in docs/01 and this module can be diffed by eye, because the table has the same
  shape as the diagram.

The FSM does not decide anything. `ARBITRATE` takes whichever trigger `policy.decide` produced,
via `trigger_for_decision`; the machine only knows which state that leads to.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from tribunal.contracts import Decision


class State(StrEnum):
    INIT = "INIT"
    GROUND = "GROUND"
    PROPOSE = "PROPOSE"
    VALIDATE = "VALIDATE"
    CRITIQUE = "CRITIQUE"
    ARBITRATE = "ARBITRATE"
    TRADEOFF = "TRADEOFF"
    ESCALATE = "ESCALATE"
    POSTMORTEM = "POSTMORTEM"
    DONE = "DONE"
    FAILED = "FAILED"


class Trigger(StrEnum):
    READY = "ready"
    GROUNDED = "grounded"
    PROPOSED = "proposed"
    #: VALIDATE outcomes.
    PATCH_OK = "patch_ok"
    PATCH_REJECTED_RETRIES_LEFT = "patch_rejected_retries_left"
    PATCH_REJECTED_EXHAUSTED = "patch_rejected_exhausted"
    CRITIQUED = "critiqued"
    #: ARBITRATE outcomes, one per `Decision`.
    REJECT = "reject"
    ACCEPT = "accept"
    TRADEOFF = "tradeoff"
    ESCALATE = "escalate"
    RECORDED = "recorded"
    WRITTEN = "written"
    #: The original file does not parse. docs/01 § States: `FAILED` means "the system could
    #: not produce anything reviewable (e.g. input doesn't parse)", and there is nothing to
    #: propose a patch against, so the run stops before spending a Coder call.
    INPUT_UNUSABLE = "input_unusable"


#: Terminal states. Nothing leaves them.
TERMINAL: frozenset[State] = frozenset({State.DONE, State.FAILED})

#: The transition table, in the order of the diagram in docs/01-architecture.md.
TRANSITIONS: dict[tuple[State, Trigger], State] = {
    (State.INIT, Trigger.READY): State.GROUND,
    (State.GROUND, Trigger.GROUNDED): State.PROPOSE,
    # Row 1's *first* clause. docs/01's diagram draws only VALIDATE -> FAILED, which covers
    # the second clause ("no diff ever applied after retries") and leaves the first with no
    # edge at all -- so `PolicyInput.input_parses` was a guard nothing could reach.
    # docs/13 § 44.
    (State.GROUND, Trigger.INPUT_UNUSABLE): State.FAILED,
    (State.PROPOSE, Trigger.PROPOSED): State.VALIDATE,
    # VALIDATE is the cheap deterministic gate. A malformed patch bounces straight back to
    # the Coder without either critic being called (docs/01 § Why VALIDATE exists).
    (State.VALIDATE, Trigger.PATCH_REJECTED_RETRIES_LEFT): State.PROPOSE,
    (State.VALIDATE, Trigger.PATCH_REJECTED_EXHAUSTED): State.FAILED,
    (State.VALIDATE, Trigger.PATCH_OK): State.CRITIQUE,
    (State.CRITIQUE, Trigger.CRITIQUED): State.ARBITRATE,
    (State.ARBITRATE, Trigger.REJECT): State.PROPOSE,
    (State.ARBITRATE, Trigger.ESCALATE): State.ESCALATE,
    (State.ARBITRATE, Trigger.TRADEOFF): State.TRADEOFF,
    (State.ARBITRATE, Trigger.ACCEPT): State.POSTMORTEM,
    # Both non-accept terminals still write a postmortem: an escalated run must produce a
    # report and the best patch so far, not lose the work.
    (State.TRADEOFF, Trigger.RECORDED): State.POSTMORTEM,
    (State.ESCALATE, Trigger.RECORDED): State.POSTMORTEM,
    (State.POSTMORTEM, Trigger.WRITTEN): State.DONE,
}

#: `policy.decide` returns a `Decision`; the machine needs a `Trigger`. One mapping, so a new
#: decision cannot be added without a transition for it.
DECISION_TRIGGERS: dict[Decision, Trigger] = {
    Decision.ACCEPT: Trigger.ACCEPT,
    Decision.REJECT: Trigger.REJECT,
    Decision.TRADEOFF: Trigger.TRADEOFF,
    Decision.ESCALATE: Trigger.ESCALATE,
}


class IllegalTransition(RuntimeError):
    """No table entry for this `(state, trigger)` pair.

    Raised rather than ignored. A silent no-op here is how a run ends in an undefined state,
    which is exactly what charter criterion S1 forbids.
    """


def next_state(state: State, trigger: Trigger) -> State:
    try:
        return TRANSITIONS[(state, trigger)]
    except KeyError:
        legal = sorted(t.value for (s, t) in TRANSITIONS if s is state)
        raise IllegalTransition(
            f"no transition from {state.value} on {trigger.value}; "
            f"legal triggers there are {legal or '(none — terminal)'}"
        ) from None


def trigger_for_decision(decision: Decision, rounds_remain: bool) -> Trigger:
    """Map a policy decision onto a trigger, with the one guard the table cannot express.

    `REJECT` means "go round again", which is only reachable while the round budget holds.
    Policy already escalates via `rounds_exhausted` when pressure is high, but a REJECT on the
    final round from a *different* row -- `correctness_regression`, say -- must not loop
    forever. The guard lives here because it is about the machine's budget, not about the
    merits of the patch.
    """
    if decision is Decision.REJECT and not rounds_remain:
        return Trigger.ESCALATE
    return DECISION_TRIGGERS[decision]


def legal_triggers(state: State) -> tuple[Trigger, ...]:
    return tuple(sorted((t for (s, t) in TRANSITIONS if s is state), key=lambda t: t.value))


def reachable_states() -> frozenset[State]:
    """Every state reachable from INIT. Used to prove no state is orphaned."""
    seen = {State.INIT}
    frontier = [State.INIT]
    while frontier:
        state = frontier.pop()
        for (source, _), target in TRANSITIONS.items():
            if source is state and target not in seen:
                seen.add(target)
                frontier.append(target)
    return frozenset(seen)


@dataclass(frozen=True)
class Machine:
    """A position in the state machine, plus the counters the guards need.

    Immutable: `step` returns a new `Machine`. That makes a run a list of values, which is
    what lets a trace be replayed into the same sequence of states.
    """

    state: State = State.INIT
    round: int = 0
    patch_attempts: int = 0
    transitions: int = 0
    history: tuple[State, ...] = (State.INIT,)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def step(self, trigger: Trigger) -> Machine:
        target = next_state(self.state, trigger)
        # Entering PROPOSE from ARBITRATE begins a new debate round; entering it from VALIDATE
        # is a re-anchoring attempt within the current round. Counting them separately is the
        # same distinction docs/03-agents.md draws between the two budgets.
        new_round = self.round
        attempts = self.patch_attempts
        if target is State.PROPOSE:
            if trigger is Trigger.PATCH_REJECTED_RETRIES_LEFT:
                attempts += 1
            else:
                new_round += 1
                attempts = 0
        elif self.state is State.GROUND and target is State.PROPOSE:  # pragma: no cover
            new_round += 1
        return replace(
            self,
            state=target,
            round=new_round,
            patch_attempts=attempts,
            transitions=self.transitions + 1,
            history=(*self.history, target),
        )
