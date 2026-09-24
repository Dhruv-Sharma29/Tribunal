"""Anthropic provider.

The only backend that reaches `StructureMode.NATIVE_STRICT` through an SDK helper:
`messages.parse(output_format=<PydanticModel>)` constrains decoding and hands back a
validated instance, so a `Critique` that violates the contract cannot be produced in the
first place.

Wire shape confirmed against the bundled `claude-api` skill (model table cached 2026-06-24):

* `thinking={"type": "adaptive"}` -- `budget_tokens` is **rejected with a 400** on Opus 5 and
  Sonnet 5. The project's docs/02-contracts.md snippet already uses the adaptive form.
* `effort` goes *inside* `output_config`, not at the top level.
* Model ids carry **no date suffix**. `claude-haiku-4-5-20251001` is wrong; `config.py` keeps
  the dated form only as an alias that resolves to `claude-haiku-4-5`.
* Assistant prefill is rejected, so there is no "start the reply with `{`" fallback --
  structured output is the only mechanism.
* `cache_control` on the system block is what makes the stable prefix cacheable. Verify it is
  working via `usage.cache_read_input_tokens`; a zero across rounds means something volatile
  leaked into the prefix (docs/06-observability.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tribunal.config import ProviderSettings, cost_usd, resolve_model
from tribunal.contracts import Usage
from tribunal.llm import schema as schema_mod
from tribunal.llm.base import (
    Capabilities,
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

CONTRACT_SOURCE = "anthropic-sdk + bundled claude-api skill (model table 2026-06-24)"


class AnthropicProvider(Provider):
    name = ProviderName.ANTHROPIC
    key_env = "ANTHROPIC_API_KEY"
    default_model = "claude-opus-5"
    contract_source = CONTRACT_SOURCE

    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings(key_env=self.key_env)
        self.key_env = self.settings.key_env

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            structure=StructureMode.NATIVE_STRICT,
            explicit_prompt_cache=True,
            reports_cache_tokens=True,
            controllable_effort=True,
            inline_reasoning=False,
        )

    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema_mod.for_anthropic(schema).schema

    def _missing_sdk(self) -> str | None:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "anthropic"
        return None

    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        import anthropic

        reason = self.available()
        if reason:
            raise ProviderUnavailable(f"anthropic: {reason}")

        client = anthropic.AsyncAnthropic(
            base_url=self.settings.base_url,
            timeout=timeout,
            max_retries=self.settings.max_retries,
        )
        model = resolve_model(request.model)
        system: list[dict[str, Any]] = [{"type": "text", "text": request.system}]
        if request.cache_system:
            # The stable prefix, and only the stable prefix. Everything volatile is in `user`.
            system[0]["cache_control"] = {"type": "ephemeral"}

        try:
            response = await client.messages.parse(
                model=model,
                max_tokens=request.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": request.effort},
                system=system,
                messages=[{"role": "user", "content": request.user}],
                output_format=output_model,
            )
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited(f"anthropic: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderTransientError(f"anthropic {exc.status_code}: {exc}") from exc
            raise ProviderBadResponse(f"anthropic {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTransientError(f"anthropic: {exc}") from exc
        finally:
            await client.close()

        if response.stop_reason == "refusal":
            # Expected traffic for a code reviewer, not an exception to paper over: the
            # critic is recorded as errored, which policy reads as `unassessed`, never `clean`.
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise ProviderRefused(f"anthropic declined the request (category={category})")

        parsed = response.parsed_output
        if parsed is None:
            raise ProviderBadResponse(
                f"anthropic returned no parsed output (stop_reason={response.stop_reason}); "
                "a max_tokens stop here means the schema did not fit in the output budget"
            )
        data = parsed.model_dump(mode="json")
        reasoning = "\n".join(
            block.thinking
            for block in response.content
            if getattr(block, "type", None) == "thinking" and getattr(block, "thinking", "")
        )
        return LLMResponse(
            data=data,
            raw_text=parsed.model_dump_json(),
            structure=StructureMode.NATIVE_STRICT,
            usage=_usage(model, response),
            stop_reason=response.stop_reason,
            reasoning=reasoning or None,
        )


def _usage(model: str, response: Any) -> Usage:
    raw = response.usage
    input_tokens = getattr(raw, "input_tokens", 0) or 0
    output_tokens = getattr(raw, "output_tokens", 0) or 0
    cache_read = getattr(raw, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(raw, "cache_creation_input_tokens", 0) or 0
    return Usage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        cost_usd=cost_usd(model, input_tokens, output_tokens, cache_read, cache_write),
        # Unrecoverable after the fact, and it is what you quote when escalating to support.
        request_id=getattr(response, "_request_id", None),
    )
