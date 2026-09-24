"""The Coder: proposes a patch, and re-anchors it when VALIDATE bounces.

## Two retry budgets, deliberately separate

docs/03-agents.md mitigation 3 says VALIDATE "returns a precise mechanical error and the Coder
retries (2 attempts, **not counted against debate rounds**)". That is a third budget, distinct
from the two already in the system, and conflating any of them would hide a different problem:

| Budget | Counts | Spent when | Owner |
|---|---|---|---|
| `repair_retries` (1) | extra LLM calls | the output does not match the schema | `LLMClient` |
| `patch_attempts_per_round` (2) | extra LLM calls | the patch does not apply or parse | here |
| `max_rounds` (3) | debate rounds | the policy layer rejects the patch | orchestrator |

A diff-formatting stumble must not consume the debate budget, and a schema violation must not
look like a bad anchor. Keeping them apart is also what makes the repair-retry rate usable as
the prompt-health signal (docs/09-roadmap.md): if VALIDATE bounces counted as schema repairs,
that signal would be dominated by line-number arithmetic.

## Why the loop lives in the agent

docs/01-architecture.md makes `VALIDATE` a state the *orchestrator* owns, and it will: the
orchestrator drives the transitions, enforces the budget, and emits the trace events. The
mechanism lives here anyway because docs/09-roadmap.md Phase 2 requires each agent to be
testable alone, and because the retry prompt belongs next to the prompt it repairs. The
orchestrator will call `propose()` and read `CoderResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tribunal.agents.base import Agent, AgentRun
from tribunal.agents.bundle import CoderBundle
from tribunal.contracts import Patch, PatchProposal, PatchValidation
from tribunal.patch import validate as validate_patch


@dataclass
class CoderAttempt:
    """One proposal and what VALIDATE made of it."""

    run: AgentRun[PatchProposal]
    patch: Patch
    validation: PatchValidation

    @property
    def ok(self) -> bool:
        return self.validation.ok

    @property
    def patched_source(self) -> str | None:
        return self.validation.patched_source


@dataclass
class CoderResult:
    """Every attempt, and the one that passed. `accepted is None` means FAILED."""

    attempts: list[CoderAttempt] = field(default_factory=list)

    @property
    def accepted(self) -> CoderAttempt | None:
        return next((a for a in self.attempts if a.ok), None)

    @property
    def ok(self) -> bool:
        return self.accepted is not None

    @property
    def patch_attempts(self) -> int:
        """Counted separately from debate rounds, and reported separately in the trace."""
        return len(self.attempts)

    @property
    def cost_usd(self) -> float:
        return sum(a.run.cost_usd for a in self.attempts)

    @property
    def failure_reason(self) -> str | None:
        if self.ok or not self.attempts:
            return None
        return self.attempts[-1].validation.failure_reason

    def trace_payload(self) -> dict:
        """One `patch_validate` event per attempt, plus the aggregate."""
        return {
            "patch_attempts": self.patch_attempts,
            "accepted": self.ok,
            "failure_reason": self.failure_reason,
            "attempts": [
                {
                    "applied": a.validation.applied,
                    "parse_ok": a.validation.parse_ok,
                    "hunks": a.validation.hunks,
                    "diff_sha256": a.validation.diff_sha256,
                    "failure_reason": a.validation.failure_reason,
                    "addresses": a.patch.addresses,
                    "declined": [u.issue_id for u in a.patch.deliberately_unaddressed],
                    "parse_retries": a.run.outcome.repair_retries,
                }
                for a in self.attempts
            ],
        }


class Coder(Agent[PatchProposal]):
    role = "coder"
    output_model = PatchProposal

    def render_user(self, bundle: CoderBundle) -> str:
        return bundle.render()

    def post_validate(self, value: PatchProposal, bundle: CoderBundle) -> None:
        from tribunal.agents.validation import validate_patch_proposal

        validate_patch_proposal(value, bundle)

    def metrics(self, value: PatchProposal, bundle: CoderBundle) -> dict:
        return {
            "edits": len(value.edits),
            "used_diff_fallback": bool(value.diff.strip()),
            "addresses": len(value.addresses),
            "declined": len(value.deliberately_unaddressed),
            "empty": value.is_empty,
        }

    async def propose(
        self, bundle: CoderBundle, max_attempts: int | None = None
    ) -> CoderResult:
        """Propose, validate, and re-anchor on a mechanical failure.

        Stops at the first patch that applies and parses. A bounce carries the exact error
        forward -- "hunk 2 failed to apply at line 47", "search block matches 2 places" -- so
        the retry is a re-anchoring exercise, not another guess at the fix.
        """
        budget = max_attempts or self.settings.policy.patch_attempts_per_round
        result = CoderResult()
        current = bundle

        for _ in range(budget):
            run = await self.run(current, round_=bundle.round)
            patch, validation = validate_patch(
                bundle.source,
                run.value,
                filename=bundle.filename,
                max_hunks=bundle.max_hunks,
            )
            result.attempts.append(
                CoderAttempt(run=run, patch=patch, validation=validation)
            )
            if validation.ok:
                return result
            # `failure_reason` is always set when not ok -- the PatchValidation invariant.
            current = current.with_validation_error(
                validation.failure_reason or "unknown validation failure", patch.diff
            )
        return result
