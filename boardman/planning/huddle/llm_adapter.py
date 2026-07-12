from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from boardman.llm.completion import chat_complete
from boardman.llm.ollama_autodetect import effective_ollama_model
from boardman.settings import settings


@dataclass(slots=True)
class GenerateResult:
    text: str
    provider: str
    model: str


class PlanningLlm(Protocol):
    def generate(self, prompt: str) -> GenerateResult: ...


@dataclass(slots=True)
class BoardmanPlanningLlm:
    """Sync wrapper around boardman ``chat_complete`` for meeting plan generation."""

    provider: str | None = None
    model: str | None = None
    timeout: float = 120.0

    def generate(self, prompt: str) -> GenerateResult:
        prov = (self.provider or settings.llm_provider or "ollama").lower()
        mdl = self.model
        if prov == "ollama":
            mdl = effective_ollama_model(mdl)
        elif not mdl:
            mdl = settings.llm_model or "default"

        messages = [
            {
                "role": "system",
                "content": (
                    "You write facilitator-ready markdown meeting plans. "
                    "Output markdown only with ## section headings."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        text = asyncio.run(
            chat_complete(
                messages,
                provider=self.provider,
                model=self.model,
                timeout=self.timeout,
            )
        )
        return GenerateResult(text=text, provider=prov, model=str(mdl or "default"))
