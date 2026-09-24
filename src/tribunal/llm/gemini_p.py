"""Google Gemini provider, via the Interactions API.

Reaches `StructureMode.NATIVE_SCHEMA`: the schema is sent and honoured, but Gemini accepts a
documented *subset* of JSON Schema, so the guarantee is weaker than constrained decoding
against our exact document. Two adaptations in `llm/schema.py` make the schema acceptable at
all -- `$ref`/`$defs` are inlined (Gemini's keyword list does not include them) and
`additionalProperties` is stripped (not supported).

**The wire shape here came from SDK introspection, not from the prose docs.** The
structured-output guide shows a flat
`response_format={"type": "text", "mime_type": ..., "schema": ...}`, but
`google.genai.types.ResponseFormat` actually nests a `TextResponseFormat` with fields
`mime_type` and `jsonSchema` -- camelCase, and under a `text` key. Following the prose would
have produced a silently ignored schema, which is the worst failure mode available here: a
response that parses as JSON but does not match the contract.

`client.aio.interactions` is used rather than the sync surface wrapped in
`asyncio.to_thread`, and that is a correctness requirement rather than a style preference:
`sandbox.py` sets rlimits through `preexec_fn`, which is unsafe in a multi-threaded parent.
A thread-pool call anywhere in the LLM layer leaves those threads alive for the rest of the
run, so the next sandboxed `pytest` would fork from a multi-threaded process.
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
from tribunal.llm.extract import parse_json_payload


class GeminiProvider(Provider):
    name = ProviderName.GEMINI
    key_env = "GEMINI_API_KEY"
    default_model = "gemini-3.8-flash"
    contract_source = "google-genai 2.23 SDK introspection (2026-09-16); NOT live-tested"

    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings(key_env=self.key_env)
        self.key_env = self.settings.key_env

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            structure=StructureMode.NATIVE_SCHEMA,
            # Gemini has explicit context caching, but it is a separate cached-content
            # resource with per-hour storage billing rather than a request-side breakpoint,
            # so the client has nothing to arrange on a per-call basis.
            explicit_prompt_cache=False,
            reports_cache_tokens=True,
            controllable_effort=False,
            inline_reasoning=False,
        )

    def adapt_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        return schema_mod.for_gemini(schema).schema

    def _missing_sdk(self) -> str | None:
        try:
            from google import genai  # noqa: F401
        except ImportError:
            return "gemini"
        return None

    async def complete(
        self, request: LLMRequest, output_model: type[BaseModel], timeout: float
    ) -> LLMResponse:
        import os

        from google import genai
        from google.genai import errors as genai_errors

        reason = self.available()
        if reason:
            raise ProviderUnavailable(f"gemini: {reason}")

        client = genai.Client(api_key=os.environ[self.key_env])
        model = resolve_model(request.model)
        try:
            interaction = await client.aio.interactions.create(
                model=model,
                input=request.user,
                system_instruction=request.system,
                # `jsonSchema`, camelCase, nested under `text`. Verified by introspecting
                # google.genai.types.TextResponseFormat, not copied from the prose docs.
                response_format={
                    "text": {
                        "mime_type": "application/json",
                        "jsonSchema": request.schema_,
                    }
                },
                timeout=timeout,
            )
        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                raise ProviderRateLimited(f"gemini: {exc}") from exc
            raise ProviderBadResponse(f"gemini {code}: {exc}") from exc
        except genai_errors.ServerError as exc:
            raise ProviderTransientError(f"gemini: {exc}") from exc
        finally:
            await client.aio.aclose()

        status = getattr(interaction, "status", None)
        status_name = getattr(status, "value", status)
        if getattr(interaction, "errors", None):
            raise ProviderBadResponse(f"gemini returned errors: {interaction.errors}")
        if status_name in ("BLOCKED", "blocked", "SAFETY", "safety"):
            raise ProviderRefused(f"gemini declined the request (status={status_name})")

        text = getattr(interaction, "output_text", "") or ""
        data, recovered, reasoning = parse_json_payload(text)
        return LLMResponse(
            data=data,
            raw_text=text,
            structure=StructureMode.EXTRACTED if recovered else StructureMode.NATIVE_SCHEMA,
            usage=_usage(model, interaction),
            stop_reason=str(status_name) if status_name is not None else None,
            reasoning=reasoning,
        )


def _usage(model: str, interaction: Any) -> Usage:
    raw = getattr(interaction, "usage", None)
    prompt = getattr(raw, "prompt_token_count", 0) or 0
    response_tokens = getattr(raw, "response_token_count", 0) or 0
    cached = getattr(raw, "cached_content_token_count", 0) or 0
    # Thinking tokens bill as output. Omitting them would under-report the cost of exactly
    # the models the tribunal wants for the Arbiter.
    thoughts = getattr(raw, "thoughts_token_count", 0) or 0
    output_tokens = response_tokens + thoughts
    uncached = max(0, prompt - cached)
    return Usage(
        model=model,
        input_tokens=uncached,
        output_tokens=output_tokens,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
        cost_usd=cost_usd(model, uncached, output_tokens, cached, 0),
        request_id=getattr(interaction, "id", None),
    )
