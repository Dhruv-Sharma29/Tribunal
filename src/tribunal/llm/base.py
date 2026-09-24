"""Provider-agnostic request/response types and the `Provider` interface.

tribunal's premise is that critiques are *schema-validated structures*, not prose. That puts
an unusual requirement on the LLM layer: the interesting difference between providers is not
their prompt format, it is **how strongly each can guarantee the output matches a schema**.

Four levels, and the layer records which one was used on every response:

| `StructureMode` | Guarantee | Where |
|---|---|---|
| `native_strict` | decoding constrained to our exact schema | Anthropic, OpenAI `strict` |
| `native_schema` | schema honoured, documented *subset* only | Gemini |
| `native_json` | "emit JSON"; shape is the model's problem | NIM JSON mode |
| `extracted` | JSON recovered from prose or a `<think>` block | any fallback path |

That distinction is load-bearing for the eval. docs/09-roadmap.md makes the schema-parse retry
rate the prompt-health signal -- but if a run mixes providers, a rising retry rate could equally
mean "the prompt drifted" or "this provider cannot constrain output". Recording
`StructureMode` per response is what keeps those two attributable.

No provider SDK is imported here, or at module scope in any provider module: the SDKs are
optional extras, and `tribunal ground` must work with none of them installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tribunal.contracts import Usage


class ProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    NIM = "nim"


class StructureMode(StrEnum):
    NATIVE_STRICT = "native_strict"
    NATIVE_SCHEMA = "native_schema"
    NATIVE_JSON = "native_json"
    EXTRACTED = "extracted"

    @property
    def is_guaranteed(self) -> bool:
        """True only when the provider constrained decoding against our exact schema.

        Anything else can return well-formed JSON that violates the contract, which is a
        repair retry rather than a crash -- but the policy layer should never be handed a
        critique whose provenance is unclear.
        """
        return self is StructureMode.NATIVE_STRICT


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Capabilities(BaseModel):
    """What a provider/model pair can actually do. Reported by `tribunal providers`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structure: StructureMode
    #: Explicit prompt-cache breakpoints (Anthropic). Implicit prefix caching does not count:
    #: it needs no request-side support, so it is not something the client has to arrange.
    explicit_prompt_cache: bool
    #: Whether the provider reports cache hits in usage. Without this, docs/06's
    #: "a zero cache-read across rounds means a silent prefix invalidator" is unobservable.
    reports_cache_tokens: bool
    #: A thinking/reasoning budget or effort control exists.
    controllable_effort: bool
    #: Reasoning text arrives in the same field as the answer, so it has to be stripped
    #: before the JSON can be parsed (NVIDIA Nemotron reasoning models do this).
    inline_reasoning: bool


class LLMRequest(BaseModel):
    """One structured-output request, in provider-neutral form.

    This is also the cassette key. Every field that can change the response is here and
    nothing that cannot -- no timestamps, no run ids -- so the hash is stable across runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    provider: ProviderName
    model: str
    #: Stable, cacheable prefix: role, rubric, schema instructions. Never per-round content.
    system: str
    #: Volatile per-round content. Kept separate from `system` because the whole prompt-cache
    #: story depends on the boundary between the two being explicit rather than incidental.
    user: str
    #: JSON Schema of the expected output, already adapted to the provider's dialect.
    schema_: dict[str, Any] = Field(alias="json_schema")
    schema_name: str
    max_tokens: int = Field(gt=0)
    effort: Effort = "high"
    #: Ask the provider to cache the system prefix. Ignored where unsupported.
    cache_system: bool = True


class LLMResponse(BaseModel):
    """A provider's answer, normalised."""

    model_config = ConfigDict(extra="forbid")

    #: The parsed JSON object. Validation against the Pydantic model happens one layer up, in
    #: the agent, so that a validation failure can be fed back as a repair retry.
    data: dict[str, Any]
    raw_text: str
    structure: StructureMode
    usage: Usage
    stop_reason: str | None
    #: Reasoning summary where the provider returns one separately. Never required.
    reasoning: str | None
    #: True when this came from a cassette rather than the network.
    replayed: bool = False


class ProviderError(RuntimeError):
    """Base class for provider failures the client may retry or record."""

    retryable = False


class ProviderUnavailable(ProviderError):
    """The SDK is not installed, or no credential is configured."""


class ProviderRateLimited(ProviderError):
    retryable = True


class ProviderTransientError(ProviderError):
    retryable = True


class ProviderBadResponse(ProviderError):
    """The provider answered, but the answer is not usable as JSON."""


class ProviderRefused(ProviderError):
    """The provider declined the request on policy grounds.

    A first-class outcome rather than a generic error: a code reviewer is routinely asked to
    look at exploit-shaped code, so a refusal here is expected traffic and must be recorded
    as an errored critic -- which policy treats as `unassessed`, never as `clean`.
    """


class Provider(ABC):
    """One backend. Stateless; the client owns retries, caching and accounting."""

    name: ProviderName
    #: Environment variable holding the credential.
    key_env: str
    #: Default model when config names none.
    default_model: str

    @abstractmethod
    def capabilities(self, model: str) -> Capabilities:
        """What this provider/model pair can do. Must not require a credential or network."""

    @abstractmethod
    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Rewrite a Pydantic-generated JSON Schema into this provider's dialect."""

    @abstractmethod
    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        """Issue the request. Raises a `ProviderError` subclass on failure.

        `output_model` is passed alongside the already-adapted `request.schema_` because the
        Anthropic SDK's `messages.parse` takes the Pydantic class directly and is the only
        path to constrained decoding. It is deliberately *not* a field on `LLMRequest`: the
        request is the cassette key, and a class object is not hashable content.
        """

    #: Where this provider's wire shape was verified. Printed by `tribunal providers`, so
    #: nobody has to guess which backends have been exercised against a live endpoint.
    contract_source: str = "unverified"

    def available(self) -> str | None:
        """Return None if usable, else a human-readable reason it is not.

        Checked by `tribunal providers` and `doctor`, so it must not make a network call.
        """
        import os

        if not os.environ.get(self.key_env):
            return f"{self.key_env} is not set"
        missing = self._missing_sdk()
        if missing:
            return f"missing SDK: pip install 'tribunal[{missing}]'"
        return None

    def _missing_sdk(self) -> str | None:
        """Name of the optional extra to install, or None if the SDK imports."""
        return None
