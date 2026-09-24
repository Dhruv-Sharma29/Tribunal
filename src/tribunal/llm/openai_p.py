"""OpenAI provider, via the Responses API.

Reaches `StructureMode.NATIVE_STRICT`: with `strict: true`, decoding is constrained to the
schema. Two requirements the docs are explicit about, both handled in `llm/schema.py`:
`additionalProperties: false` on every object, and `required` listing *every* key at each
level (optionality is expressed as a nullable union, not by omission).

Wire shape from https://developers.openai.com/api/docs/guides/structured-outputs:
`text={"format": {"type": "json_schema", "strict": True, "schema": ...}}`. `responses.create`
is used rather than `responses.parse` so that one dict-schema code path serves all four
providers and the cassette key has a single shape; validation against the Pydantic model
happens a layer up, where a failure can become a repair retry.

Effort is mapped, not passed through: OpenAI's reasoning effort does not have the project's
`xhigh`/`max` levels, so those clamp to `high` rather than 400-ing.
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
    ProviderRefused,
    ProviderTransientError,
    ProviderUnavailable,
    StructureMode,
)
from tribunal.llm.extract import parse_json_payload

#: OpenAI reasoning effort has no `xhigh`/`max`. Clamping is better than a 400, and better
#: than silently dropping the parameter -- the Arbiter is configured at `xhigh` by default.
EFFORT_MAP: dict[Effort, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class OpenAIProvider(Provider):
    name = ProviderName.OPENAI
    key_env = "OPENAI_API_KEY"
    default_model = "gpt-6-astra"
    contract_source = "developers.openai.com structured-outputs guide (fetched 2026-09-16)"

    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings(key_env=self.key_env)
        self.key_env = self.settings.key_env

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            structure=StructureMode.NATIVE_STRICT,
            # Prefix caching is automatic and needs no request-side support, so there is
            # nothing for the client to arrange -- but it *is* reported in usage.
            explicit_prompt_cache=False,
            reports_cache_tokens=True,
            controllable_effort=True,
            inline_reasoning=False,
        )

    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema_mod.for_openai(schema).schema

    def _missing_sdk(self) -> str | None:
        try:
            import openai  # noqa: F401
        except ImportError:
            return "openai"
        return None

    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        import openai

        reason = self.available()
        if reason:
            raise ProviderUnavailable(f"openai: {reason}")

        client = openai.AsyncOpenAI(
            base_url=self.settings.base_url,
            timeout=timeout,
            max_retries=self.settings.max_retries,
        )
        model = resolve_model(request.model)
        try:
            response = await client.responses.create(
                model=model,
                instructions=request.system,
                input=request.user,
                max_output_tokens=request.max_tokens,
                reasoning={"effort": EFFORT_MAP[request.effort]},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": request.schema_name,
                        "strict": True,
                        "schema": request.schema_,
                    }
                },
            )
        except openai.RateLimitError as exc:
            raise ProviderRateLimited(f"openai: {exc}") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderTransientError(f"openai {exc.status_code}: {exc}") from exc
            raise ProviderBadResponse(f"openai {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderTransientError(f"openai: {exc}") from exc
        finally:
            await client.close()

        text = getattr(response, "output_text", "") or ""
        status = getattr(response, "status", None)
        if getattr(response, "refusal", None) or status == "refused":
            raise ProviderRefused("openai declined the request")
        if status == "incomplete":
            detail = getattr(getattr(response, "incomplete_details", None), "reason", None)
            raise ProviderBadResponse(f"openai returned an incomplete response ({detail})")

        data, recovered, reasoning = parse_json_payload(text)
        return LLMResponse(
            data=data,
            raw_text=text,
            # Strict mode should never need recovery; if it did, say so rather than claiming
            # a guarantee the response did not actually honour.
            structure=StructureMode.EXTRACTED if recovered else StructureMode.NATIVE_STRICT,
            usage=_usage(model, response),
            stop_reason=status,
            reasoning=reasoning,
        )


def _usage(model: str, response: Any) -> Usage:
    raw = getattr(response, "usage", None)
    input_tokens = getattr(raw, "input_tokens", 0) or 0
    output_tokens = getattr(raw, "output_tokens", 0) or 0
    details = getattr(raw, "input_tokens_details", None)
    cache_read = getattr(details, "cached_tokens", 0) or 0
    # OpenAI's cached tokens are reported *inside* input_tokens, unlike Anthropic where they
    # are a separate bucket. Subtracting keeps the cost arithmetic from billing them twice.
    uncached = max(0, input_tokens - cache_read)
    return Usage(
        model=model,
        input_tokens=uncached,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0,  # no separate write charge on automatic prefix caching
        cost_usd=cost_usd(model, uncached, output_tokens, cache_read, 0),
        request_id=getattr(response, "id", None),
    )
