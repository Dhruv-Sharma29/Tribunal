"""NVIDIA NIM provider (OpenAI-compatible), including the Nemotron reasoning models.

Default model: `nvidia/llama-3.3-nemotron-super-49b-v1.5`, a dense 49B reasoning model.

It replaced `nemotron-3-nano-omni-30b-a3b-reasoning`, which is still priced and still a
legitimate choice. The nano model is 30B *A3B* -- a mixture of experts with roughly 3B
active parameters per token -- and every judgement this system asks for is the kind that
scales with active parameters rather than with context: re-rating a finding in context,
deciding whether two remedies genuinely conflict, adjudicating a Coder's pushback. The
nano model is faster and cheaper in credits and is the better pick for a smoke run; the
super model is the better pick when the output is a number someone will quote.

**Everything measured in docs/13 about "Nemotron" was measured on the nano model**, and
those figures are labelled accordingly -- the Coder's 5/6 on the fixture set, the
quote-mangling in dense replacements (§ 18, § 20), the first-run `tools_consulted`
confusion. None of them has been re-measured here, and a default change does not transfer
a measurement.

Three things make this the most interesting provider to integrate, and all three are handled
rather than hoped away:

1. **The endpoint documents JSON output, not strict schema validation.** So the schema travels
   as a *hint* and the response is reported as `StructureMode.NATIVE_JSON` at best -- never
   `NATIVE_STRICT`. Sending the schema costs nothing and helps when the backend honours it;
   relying on it would be a lie the policy layer would inherit.
2. **Reasoning placement differs between hosted and self-hosted.** NVIDIA's model docs
   describe the chain of thought as "before the final answer, visible in `content`" -- but
   probing the hosted endpoint shows otherwise: it returns reasoning in a separate
   `reasoning_content` field (1-3 KB of it) and leaves `content` as clean JSON. Measured, not
   assumed. Both shapes are handled: `reasoning_content` is preferred when present, and
   `llm/extract.py` strips an inline `<think>` block otherwise. When that recovery is needed
   the mode drops to `EXTRACTED`, which is the signal the eval uses to attribute a parse-retry
   rate to a provider rather than to prompt drift.
3. **It is not token-metered.** The hosted developer endpoint is credit-based and self-hosted
   NIM has no per-token price, so `config.PRICES` marks it `not_token_metered` rather than
   pricing it at zero -- a distinct state from "we forgot to pin a price".

## Parameters, as measured against the hosted endpoint on 2026-09-16

| Parameter | Hosted endpoint |
|---|---|
| `reasoning_effort` | accepted -- this is the depth control to use |
| `response_format: {"type": "json_object"}` | accepted |
| `response_format: {"type": "json_schema", ...}` | accepted |
| `chat_template_kwargs: {"enable_thinking": false}` | accepted; suppresses `reasoning_content` |
| `thinking_token_budget` | **rejected**: `Unsupported parameter(s)` -- self-hosted NIM only |

NVIDIA's model documentation describes `thinking_token_budget`, and sending it to the hosted
endpoint is a hard 400. It is therefore gated on a non-default `base_url`.

**503 `ResourceExhausted` is routine**, not exceptional: the shared hosted endpoint returns
`Worker local total request limit reached (16/16)` under trivial concurrency. It maps to
`ProviderTransientError` so the client retries it with backoff -- without that, a debate round
would fail for a reason that has nothing to do with the code under review.

A self-hosted NIM container is reached by overriding `base_url` (e.g.
`http://localhost:8000/v1`); the hosted endpoint is the default.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tribunal.config import ProviderSettings, cost_usd, resolve_model
from tribunal.contracts import Usage
from tribunal.llm import schema as schema_mod
from tribunal.llm.base import (
    Capabilities,
    Effort,
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderBadResponse,
    ProviderName,
    ProviderRateLimited,
    ProviderTransientError,
    ProviderUnavailable,
    StructureMode,
)
from tribunal.llm.extract import parse_json_payload

HOSTED_BASE_URL = "https://integrate.api.nvidia.com/v1"

#: The hosted endpoint takes OpenAI's `reasoning_effort`, which has no `xhigh`/`max`.
REASONING_EFFORT: dict[Effort, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

#: Self-hosted NIM additionally accepts an explicit budget. Nemotron's documented reasoning
#: budget tops out at 16,384 tokens, so the effort levels map onto that range.
THINKING_BUDGET: dict[Effort, int] = {
    "low": 2_048,
    "medium": 4_096,
    "high": 8_192,
    "xhigh": 12_288,
    "max": 16_384,
}


class NIMProvider(Provider):
    name = ProviderName.NIM
    key_env = "NVIDIA_API_KEY"
    default_model = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    contract_source = "docs.api.nvidia.com + build.nvidia.com (fetched 2026-09-16)"

    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings(
            key_env=self.key_env, base_url=HOSTED_BASE_URL
        )
        self.key_env = self.settings.key_env

    @property
    def is_hosted(self) -> bool:
        return (self.settings.base_url or HOSTED_BASE_URL) == HOSTED_BASE_URL

    def capabilities(self, model: str) -> Capabilities:
        reasoning_model = "reasoning" in model.lower() or "nemotron" in model.lower()
        return Capabilities(
            structure=StructureMode.NATIVE_JSON,
            explicit_prompt_cache=False,
            reports_cache_tokens=False,
            controllable_effort=reasoning_model,
            # Measured: the hosted endpoint separates reasoning into `reasoning_content`.
            # A self-hosted build may inline it, which is what the docs describe.
            inline_reasoning=reasoning_model and not self.is_hosted,
        )

    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema_mod.for_nim(schema).schema

    def _missing_sdk(self) -> str | None:
        try:
            import openai  # noqa: F401 - NIM speaks the OpenAI protocol
        except ImportError:
            return "openai"
        return None

    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        import openai

        reason = self.available()
        if reason:
            raise ProviderUnavailable(f"nim: {reason}")

        client = openai.AsyncOpenAI(
            api_key=_key(self.key_env),
            base_url=self.settings.base_url or HOSTED_BASE_URL,
            timeout=timeout,
            max_retries=self.settings.max_retries,
        )
        model = resolve_model(request.model)
        caps = self.capabilities(model)
        extra: dict[str, Any] = {}
        kwargs: dict[str, Any] = {}
        if caps.controllable_effort:
            kwargs["reasoning_effort"] = REASONING_EFFORT[request.effort]
            if not self.is_hosted:
                # Hard 400 on the hosted endpoint; only self-hosted NIM accepts it.
                extra["thinking_token_budget"] = THINKING_BUDGET[request.effort]

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                max_tokens=request.max_tokens,
                **kwargs,
                # Sent as a hint. The hosted endpoint documents JSON output but not strict
                # schema enforcement, so the parse below never assumes it was honoured.
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": True,
                        "schema": request.schema_,
                    },
                },
                extra_body=extra or None,
            )
        except openai.RateLimitError as exc:
            raise ProviderRateLimited(f"nim: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 400 and "response_format" in str(exc):
                # Older or self-hosted NIM builds reject json_schema. Fall back to plain JSON
                # mode rather than failing the round: the parse path is identical either way.
                response = await self._retry_json_object(client, request, model, extra, kwargs)
            elif exc.status_code >= 500:
                raise ProviderTransientError(f"nim {exc.status_code}: {exc}") from exc
            else:
                raise ProviderBadResponse(f"nim {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderTransientError(f"nim: {exc}") from exc
        finally:
            await client.close()

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise ProviderBadResponse("nim returned no choices")
        text = choice.message.content or ""
        # Some NIM builds do expose a separate reasoning field; prefer it when present.
        native_reasoning = getattr(choice.message, "reasoning_content", None)

        data, recovered, inline_reasoning = parse_json_payload(text)
        return LLMResponse(
            data=data,
            raw_text=text,
            structure=StructureMode.EXTRACTED if recovered else StructureMode.NATIVE_JSON,
            usage=_usage(model, response),
            stop_reason=choice.finish_reason,
            reasoning=native_reasoning or inline_reasoning,
        )

    async def _retry_json_object(
        self,
        client: Any,
        request: LLMRequest,
        model: str,
        extra: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> Any:
        return await client.chat.completions.create(
            model=model,
            **kwargs,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{request.system}\n\nRespond with a single JSON object matching this "
                        f"schema. Emit no prose outside the object.\n{request.schema_}"
                    ),
                },
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_tokens,
            response_format={"type": "json_object"},
            extra_body=extra or None,
        )


def _key(env: str) -> str:
    import os

    return os.environ.get(env, "")


def _usage(model: str, response: Any) -> Usage:
    raw = getattr(response, "usage", None)
    input_tokens = getattr(raw, "prompt_tokens", 0) or 0
    output_tokens = getattr(raw, "completion_tokens", 0) or 0
    return Usage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        # Legitimately 0.0: the row is marked `not_token_metered`, not left unpriced.
        cost_usd=cost_usd(model, input_tokens, output_tokens),
        request_id=getattr(response, "id", None),
    )
