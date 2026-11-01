"""The seam to a language-model provider (S3-01). No provider or key exists yet (cutover item C2), so the default is a
client that refuses; the real one will implement this protocol and nothing else changes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, Union


@dataclass(frozen=True)
class LlmResponse:
    text: str
    input_tokens: int          # as REPORTED by the provider: these, not estimates, go into the cost ledger
    output_tokens: int


class LlmNotConfigured(RuntimeError):
    pass


class LlmClient(Protocol):
    def complete(self, *, model: str, system: str, prompt: str, max_output_tokens: int) -> LlmResponse: ...


class NotConfiguredLlmClient:
    def complete(self, *, model: str, system: str, prompt: str, max_output_tokens: int) -> LlmResponse:
        raise LlmNotConfigured("no language-model provider is configured")


class FakeLlmClient:
    """Scripted responses for tests; records every call."""

    def __init__(self, *script: Union[LlmResponse, BaseException]):
        self.script: List[Union[LlmResponse, BaseException]] = list(script)
        self.calls: List[dict] = []

    def complete(self, *, model: str, system: str, prompt: str, max_output_tokens: int) -> LlmResponse:
        self.calls.append({"model": model, "system": system, "prompt": prompt, "max_output_tokens": max_output_tokens})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
