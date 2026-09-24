"""The shared agent base.

docs/03-agents.md § Shared base assigns four behaviours here so no agent reimplements them:

1. **Prompt rendering** from a `.md` template with a frontmatter-declared version.
2. **Cache-friendly request layout** -- stable system prompt and rubric first, volatile
   per-round content last. Nothing volatile may appear in the prefix.
3. **One repair retry** on validation failure, with the error appended.
4. **Trace emission** (Phase 3; the `AgentRun` below carries everything the event will need).

3 lives in `LLMClient`, which owns the retry budget and the accounting. This class owns 1, 2
and the assembly of 4, plus the cross-field evidence validation that the schema cannot express.

## Why the system/user split is load-bearing

`LLMRequest.system` is the cacheable prefix and `LLMRequest.user` is the volatile remainder.
That boundary is the whole prompt-caching story: a byte change anywhere in the prefix
invalidates everything after it, so nothing per-round -- no round number, no findings, no
timestamps, no unsorted `json.dumps` -- may cross into `system`. `_check_prefix_is_stable`
asserts that at construction rather than leaving it to a code review.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from tribunal.agents import prompts
from tribunal.agents.bundle import CritiqueBundle
from tribunal.config import AgentConfig, Settings
from tribunal.llm.base import Effort, LLMResponse, ProviderName
from tribunal.llm.client import CallOutcome, LLMClient

T = TypeVar("T", bound=BaseModel)

#: Patterns that must never appear in a cached system prefix. Each one is a silent
#: cache-invalidator: it changes between rounds or between runs, so the prefix never matches
#: and `cache_read_input_tokens` stays at zero -- which docs/06 warns you will not notice
#: until the bill arrives.
VOLATILE_IN_PREFIX = (
    re.compile(r"\bRound \d+\b"),
    re.compile(r"\b(?:19|20)\d\d-\d\d-\d\dT"),  # an ISO timestamp, not a `changed:` date
    # A run id, finding id or diff hash. Requires a digit: an all-letter run from the hex
    # alphabet is far more likely to be an English word than an identifier.
    re.compile(r"\b(?=[0-9a-f]{8,}\b)[a-f]*[0-9][0-9a-f]*\b"),
)


class PrefixNotStable(RuntimeError):
    """Something per-round leaked into the cacheable system prefix."""


@dataclass
class AgentRun(Generic[T]):
    """One agent invocation, with everything the trace needs.

    Returned instead of a bare model so that `repair_retries`, `local_repairs` and
    `prompt_version` survive to the trace. docs/09-roadmap.md makes the repair-retry rate the
    prompt-health signal, and it cannot be recovered after the fact.
    """

    value: T
    role: str
    prompt_version: str
    provider: ProviderName
    model: str
    effort: Effort
    outcome: CallOutcome
    duration_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def response(self) -> LLMResponse:
        return self.outcome.response

    @property
    def cost_usd(self) -> float:
        return self.outcome.cost_usd

    def trace_payload(self) -> dict[str, Any]:
        """The `llm_response` event payload for this run."""
        return {
            "role": self.role,
            "prompt_version": self.prompt_version,
            "provider": self.provider.value,
            "model": self.model,
            "effort": self.effort,
            "structure": self.response.structure.value,
            "structure_guaranteed": self.response.structure.is_guaranteed,
            "parse_retries": self.outcome.repair_retries,
            "local_repairs": self.outcome.local_repairs,
            "transient_retries": self.outcome.transient_retries,
            "dropped_constraints": self.outcome.dropped_constraints,
            "request_hash": self.outcome.request_hash,
            "replayed": self.response.replayed,
            "duration_ms": self.duration_ms,
            "metrics": self.metrics,
        }


class Agent(ABC, Generic[T]):
    """A versioned prompt, a declared output schema, and a model/effort setting."""

    role: str
    output_model: type[T]

    def __init__(self, settings: Settings, client: LLMClient | None = None) -> None:
        self.settings = settings
        self.client = client or LLMClient(settings)
        self.prompt = prompts.load(self.role)
        self.config: AgentConfig = getattr(settings.agents, self.role)
        self._check_prefix_is_stable(self.system_prompt())

    # -- identity -----------------------------------------------------------------------

    @property
    def prompt_version(self) -> str:
        return self.prompt.stamp

    @property
    def provider(self) -> ProviderName:
        return self.config.resolved_provider()

    @property
    def model(self) -> str:
        return self.config.model

    def effort_for(self, round_: int) -> Effort:
        return self.config.effort_for_round(round_)

    # -- prompt -------------------------------------------------------------------------

    def system_prompt(self) -> str:
        """The cacheable prefix: role, rubric, field rules. Identical across every round."""
        return self.prompt.text

    @abstractmethod
    def render_user(self, bundle: Any) -> str:
        """The volatile remainder."""

    def post_validate(self, value: T, bundle: Any) -> None:
        """Cross-field checks the schema cannot express. Raise `ValueError` to repair."""

    def metrics(self, value: T, bundle: Any) -> dict[str, Any]:
        return {}

    @staticmethod
    def _check_prefix_is_stable(system: str) -> None:
        for pattern in VOLATILE_IN_PREFIX:
            match = pattern.search(system)
            if match:
                raise PrefixNotStable(
                    f"the system prompt contains {match.group(0)!r}, which varies per round or "
                    "per run. Anything volatile in the cached prefix invalidates the whole "
                    "cache; move it into the rendered user content."
                )

    # -- run ----------------------------------------------------------------------------

    async def run(self, bundle: Any, round_: int | None = None) -> AgentRun[T]:
        round_ = round_ if round_ is not None else getattr(bundle, "round", 1)
        started = time.monotonic()
        outcome = await self.client.call(
            provider=self.provider,
            model=self.model,
            system=self.system_prompt(),
            user=self.render_user(bundle),
            output_model=self.output_model,
            max_tokens=self.config.max_tokens,
            effort=self.effort_for(round_),
            post_validate=lambda value: self.post_validate(value, bundle),
        )
        value: T = outcome.value  # type: ignore[assignment]
        return AgentRun(
            value=value,
            role=self.role,
            prompt_version=self.prompt_version,
            provider=self.provider,
            model=self.model,
            effort=self.effort_for(round_),
            outcome=outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
            metrics=self.metrics(value, bundle),
        )


class Critic(Agent[Any]):
    """A dimension-scoped critic. Red-team and Profiler differ only in prompt and dimension."""

    dimension: Any

    def render_user(self, bundle: CritiqueBundle) -> str:
        if bundle.dimension is not self.dimension:
            raise ValueError(
                f"{self.role} assesses {self.dimension.value}, but the bundle is for "
                f"{bundle.dimension.value}"
            )
        return bundle.render()

    def post_validate(self, value: Any, bundle: CritiqueBundle) -> None:
        from tribunal.agents.identity import canonicalise_issue_ids, finding_rules
        from tribunal.agents.validation import validate_critique

        # Canonicalise first, so validation and everything downstream see stable,
        # content-derived ids rather than whatever the model invented. Observed model output
        # includes "1"/"2" and the grounding finding id reused verbatim -- both schema-valid,
        # both fatal to dismissal tracking and conflict detection.
        self.last_id_mapping = canonicalise_issue_ids(
            value, finding_rules(bundle.patched_report)
        )
        validate_critique(value, bundle)

    #: Populated by `post_validate`: [(model_id, canonical_id)] in issue order. A list, not a
    #: dict: a model that emits "1" twice would collapse two renames into one.
    last_id_mapping: list[tuple[str, str]] = []

    def metrics(self, value: Any, bundle: CritiqueBundle) -> dict[str, Any]:
        from tribunal.agents.validation import critic_metrics

        computed = critic_metrics(value, bundle)
        computed["model_supplied_ids"] = [old for old, _ in self.last_id_mapping]
        severity = computed.get("highest_severity")
        if severity is not None:
            computed["highest_severity"] = severity.value
        computed["verdict"] = value.verdict
        return computed
