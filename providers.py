"""Provider adapters.

The default provider is a deterministic mock: it needs no API key, makes no
network call, and returns the same output for the same input, so the whole
gateway runs offline. Real-provider adapters are documented stubs: they refuse
to run unless their API key is present in the environment, and they never
accept a key from code or config. Wiring a real SDK call is a one-function
change behind the same interface.
"""

from __future__ import annotations

import os


class ProviderError(RuntimeError):
    """Raised when a provider cannot serve a request (not configured, etc.)."""


class MockProvider:
    """Deterministic canned responder for offline use and tests."""

    name = "mock"

    def complete(self, prompt: str) -> str:
        snippet = " ".join(prompt.split())[:120]
        return (
            "[mock] I received your prompt and this is a deterministic "
            f"placeholder response. Prompt begins: {snippet!r}"
        )


class OpenAIProvider:
    """Stub for an OpenAI-compatible chat model.

    To enable for real, set OPENAI_API_KEY in the environment and implement
    the SDK call here. This stub intentionally performs no network I/O.
    """

    name = "openai"

    def complete(self, prompt: str) -> str:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ProviderError(
                "openai provider is not configured: set OPENAI_API_KEY "
                "in the environment (never in code or config)."
            )
        raise ProviderError(
            "openai provider stub: SDK call not implemented in this "
            "portfolio build; the mock provider serves all demo traffic."
        )


class AnthropicProvider:
    """Stub for an Anthropic-compatible chat model.

    To enable for real, set ANTHROPIC_API_KEY in the environment and
    implement the SDK call here. This stub intentionally performs no
    network I/O.
    """

    name = "anthropic"

    def complete(self, prompt: str) -> str:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError(
                "anthropic provider is not configured: set "
                "ANTHROPIC_API_KEY in the environment (never in code or "
                "config)."
            )
        raise ProviderError(
            "anthropic provider stub: SDK call not implemented in this "
            "portfolio build; the mock provider serves all demo traffic."
        )


PROVIDERS = {
    "mock": MockProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
}


def get_provider(name: str):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ProviderError(f"unknown provider: {name!r}") from None
