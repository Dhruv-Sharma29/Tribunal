"""Provider construction and discovery.

Kept separate from `client.py` so that `tribunal providers` and `doctor` can report on every
backend without instantiating a client or touching a credential.
"""

from __future__ import annotations

from tribunal.config import ProvidersConfig
from tribunal.llm.anthropic_p import AnthropicProvider
from tribunal.llm.base import Provider, ProviderName
from tribunal.llm.gemini_p import GeminiProvider
from tribunal.llm.nim_p import NIMProvider
from tribunal.llm.openai_p import OpenAIProvider

PROVIDER_CLASSES = {
    ProviderName.ANTHROPIC: AnthropicProvider,
    ProviderName.OPENAI: OpenAIProvider,
    ProviderName.GEMINI: GeminiProvider,
    ProviderName.NIM: NIMProvider,
}


def build(provider: ProviderName, config: ProvidersConfig | None = None) -> Provider:
    config = config or ProvidersConfig()
    return PROVIDER_CLASSES[provider](config.for_provider(provider))


def build_all(config: ProvidersConfig | None = None) -> dict[ProviderName, Provider]:
    config = config or ProvidersConfig()
    return {name: build(name, config) for name in PROVIDER_CLASSES}
