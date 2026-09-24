"""The provider-agnostic client: schema adaptation, retries, cassettes, repair, accounting.

One class owns the four behaviours docs/03-agents.md § Shared base assigns to the agent base,
so that no agent reimplements them and no provider has to care about them:

1. **Schema adaptation** -- the Pydantic model's JSON Schema is rewritten into the target
   provider's dialect, and the constraints that had to be dropped are reported.
2. **Retries** -- transient failures and rate limits, with backoff. NVIDIA's shared hosted
   endpoint returns `503 ResourceExhausted` under trivial concurrency, so this is not a
   theoretical concern: without it a debate round fails for reasons unrelated to the code
   under review.
3. **Cassettes** -- record and replay, keyed by the full request.
4. **Repair** -- one retry on schema-validation failure with the error appended, plus a *free*
   local repair pass first.

## The free repair pass

No provider's constrained decoding enforces `multipleOf` (see `llm/schema.py`), so a model
picking `confidence: 0.85` where the grid allows `0.8` produces a payload that looks valid and
is not. Spending a whole extra call on that would be waste, so `contracts.quantise_confidence`
is applied locally first and a repair retry is only issued if the payload is *still* invalid.

This matters for the eval, not just the bill: docs/09-roadmap.md makes the schema-parse retry
rate the prompt-health signal. If quantisation noise counted as a retry, that signal would be
dominated by a constraint no provider can honour, and prompt regressions would hide inside it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from tribunal.config import Settings
from tribunal.contracts import ArbiterNote, Critique, PatchProposal, quantise_confidence
from tribunal.llm import registry
from tribunal.llm.base import (
    Effort,
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderError,
    ProviderName,
)
from tribunal.llm.cassette import CassetteStore, request_hash
from tribunal.llm.schema import AdaptedSchema, for_anthropic, for_gemini, for_nim, for_openai

T = TypeVar("T", bound=BaseModel)

ADAPTERS = {
    ProviderName.ANTHROPIC: for_anthropic,
    ProviderName.OPENAI: for_openai,
    ProviderName.GEMINI: for_gemini,
    ProviderName.NIM: for_nim,
}


class SchemaRepairFailed(RuntimeError):
    """The model could not produce a valid payload within the repair budget.

    Raised so the orchestrator records the critic as `errored` -- which policy reads as
    `unassessed`, never as `clean` (docs/04-arbitration.md rows 5/6).
    """


@dataclass
class CallOutcome:
    """A validated result plus everything the trace needs about how it was obtained."""

    value: BaseModel
    response: LLMResponse
    request_hash: str
    #: How many extra API calls the schema cost. The prompt-health signal.
    repair_retries: int = 0
    #: Repairs applied locally, at no cost. Tracked separately so they never inflate the
    #: signal above.
    local_repairs: list[str] = field(default_factory=list)
    dropped_constraints: dict[str, list[str]] = field(default_factory=dict)
    transient_retries: int = 0

    @property
    def cost_usd(self) -> float:
        return 0.0 if self.response.replayed else self.response.usage.cost_usd


@dataclass
class LLMClient:
    settings: Settings

    def __post_init__(self) -> None:
        self.providers: dict[ProviderName, Provider] = registry.build_all(
            self.settings.providers
        )
        self.cassettes = CassetteStore(self.settings.llm.cassette_dir)
        #: Running totals, so budget enforcement has a number to check.
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        self.calls = 0

    # -- schema -------------------------------------------------------------------------

    def adapt(self, provider: ProviderName, output_model: type[BaseModel]) -> AdaptedSchema:
        return ADAPTERS[provider](output_model.model_json_schema())

    def build_request(
        self,
        provider: ProviderName,
        model: str,
        system: str,
        user: str,
        output_model: type[BaseModel],
        max_tokens: int,
        effort: Effort,
    ) -> tuple[LLMRequest, AdaptedSchema]:
        adapted = self.adapt(provider, output_model)
        request = LLMRequest(
            provider=provider,
            model=model,
            system=system,
            user=user,
            json_schema=adapted.schema,
            schema_name=output_model.__name__,
            max_tokens=max_tokens,
            effort=effort,
            cache_system=self.providers[provider].capabilities(model).explicit_prompt_cache,
        )
        return request, adapted

    # -- the call -----------------------------------------------------------------------

    async def call(
        self,
        provider: ProviderName,
        model: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int = 16_000,
        effort: Effort = "high",
        post_validate: Callable[[BaseModel], None] | None = None,
    ) -> CallOutcome:
        """Issue one structured request, repairing schema and semantic failures alike.

        `post_validate` is the hook the agent layer uses for cross-field checks the schema
        cannot express on its own -- "this evidence ref resolves to a real grounding finding",
        "this measurement is citable". It raises `ValueError` to request a repair retry, so a
        semantically wrong critique costs the same one retry as a malformed one and lands in
        the same `repair_retries` counter.
        """
        request, adapted = self.build_request(
            provider, model, system, user, output_model, max_tokens, effort
        )
        outcome = await self._call_with_repair(request, output_model, user, post_validate)
        outcome.dropped_constraints = adapted.dropped_constraints
        return outcome

    async def _call_with_repair(
        self,
        request: LLMRequest,
        output_model: type[BaseModel],
        original_user: str,
        post_validate: Callable[[BaseModel], None] | None = None,
    ) -> CallOutcome:
        budget = self.settings.llm.repair_retries
        attempt_request = request
        last_error: ValidationError | None = None

        for attempt in range(budget + 1):
            response, transient = await self._dispatch(attempt_request)
            data = dict(response.data)
            local: list[str] = []
            if self.settings.llm.repair_quantisation_locally:
                local = _quantise_in_place(data)
            try:
                value = output_model.model_validate(data)
                if post_validate is not None:
                    post_validate(value)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == budget:
                    break
                # One repair retry, with the validation error appended to the conversation --
                # the mechanism docs/03-agents.md § Shared base specifies.
                attempt_request = request.model_copy(
                    update={"user": _repair_prompt(original_user, response.raw_text, exc)}
                )
                continue
            return CallOutcome(
                value=value,
                response=response,
                request_hash=request_hash(request),
                repair_retries=attempt,
                local_repairs=local,
                transient_retries=transient,
            )

        assert last_error is not None  # noqa: S101 - loop cannot exit otherwise
        raise SchemaRepairFailed(
            f"{request.provider.value}/{request.model} could not produce a valid "
            f"{output_model.__name__} in {budget + 1} attempts: {_describe(last_error)}"
        )

    async def _dispatch(self, request: LLMRequest) -> tuple[LLMResponse, int]:
        mode = self.settings.llm.mode
        if mode == "replay":
            # Fatal on a miss. Falling through to the network here is how "zero API calls in
            # CI" quietly stops being true.
            return self.cassettes.load(request), 0

        if mode == "record" and self.cassettes.has(request):
            return self.cassettes.load(request), 0

        provider = self.providers[request.provider]
        timeout = self.settings.providers.for_provider(request.provider).timeout_seconds
        response, transient = await self._with_backoff(provider, request, timeout)

        self.calls += 1
        self.total_cost_usd += response.usage.cost_usd
        self.total_tokens += response.usage.input_tokens + response.usage.output_tokens
        if mode == "record":
            self.cassettes.save(request, response)
        return response, transient

    async def _with_backoff(
        self, provider: Provider, request: LLMRequest, timeout: float
    ) -> tuple[LLMResponse, int]:
        attempts = self.settings.providers.for_provider(request.provider).max_retries + 1
        output_model = _MODEL_BY_NAME.get(request.schema_name)
        last: ProviderError | None = None
        for attempt in range(attempts):
            try:
                response = await provider.complete(
                    request, output_model or BaseModel, timeout
                )
            except ProviderError as exc:
                last = exc
                if not exc.retryable or attempt == attempts - 1:
                    raise
                # Jittered backoff. NVIDIA's hosted endpoint recovers in seconds, not
                # milliseconds, so the base delay is deliberately not sub-second.
                delay = min(2.0 * (2**attempt) + random.uniform(0, 1.0), 30.0)
                await asyncio.sleep(delay)
            else:
                return response, attempt
        raise last  # pragma: no cover - loop always returns or raises above


#: Lets the Anthropic provider be handed the real Pydantic class for `messages.parse` while
#: `LLMRequest` stays a pure, hashable cassette key -- a class object is not hashable content.
#: Prefilled from `contracts`, which imports nothing from the project and so cannot cycle.
_MODEL_BY_NAME: dict[str, type[BaseModel]] = {
    model.__name__: model for model in (Critique, PatchProposal, ArbiterNote)
}


def register_output_model(model: type[BaseModel]) -> type[BaseModel]:
    """Register an additional output model. Agents defined outside `contracts` use this."""
    _MODEL_BY_NAME[model.__name__] = model
    return model


def _quantise_in_place(data: dict[str, Any]) -> list[str]:
    """Snap off-grid confidences, returning a note per field repaired.

    Walks the payload rather than the model, because at this point the payload has not
    validated and may not have the shape the model expects.
    """
    repaired: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        value = node.get("confidence")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            snapped = quantise_confidence(float(value))
            if snapped != value:
                node["confidence"] = snapped
                repaired.append(f"{path or '<root>'}.confidence {value} -> {snapped}")
        for key, child in node.items():
            walk(child, f"{path}.{key}" if path else key)

    walk(data, "")
    return repaired


def _describe(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        return (
            f"{error.error_count()} validation error(s); first is "
            f"{first['loc']} -> {first['msg']}"
        )
    return str(error)


def _repair_prompt(original_user: str, raw_text: str, error: Exception) -> str:
    if isinstance(error, ValidationError):
        problems = "\n".join(
            f"  - {'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in error.errors()[:10]
        )
    else:
        # A semantic failure from `post_validate`. Its message is written for the model to
        # act on, so it is passed through verbatim rather than reformatted.
        problems = "\n".join(f"  - {line}" for line in str(error).splitlines())
    return (
        f"{original_user}\n\n"
        "--- Your previous response was rejected. Fix exactly these problems "
        "and re-emit the whole object. Change nothing else. ---\n"
        f"{problems}\n\n"
        f"Your previous response was:\n{raw_text[:4000]}"
    )
