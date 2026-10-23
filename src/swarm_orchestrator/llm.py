"""Pluggable chat models. The mock is deterministic and needs no API key."""

from __future__ import annotations

import time
from typing import Protocol

import httpx

from swarm_orchestrator.retry import RateLimitError, retry_delay
from swarm_orchestrator.settings import Settings


class LLM(Protocol):
    def complete(self, prompt: str) -> str: ...


class MockLLM:
    """Stable replies so plans, tests, and the demo do not depend on a provider."""

    def complete(self, prompt: str) -> str:
        text = prompt.lower()
        if "low-confidence" in text:
            return (
                "The draft is internally consistent, but the evidence cited in the "
                "goal is thin. Confidence is intentionally below the review threshold."
            )
        if "scenario" in text or "maneuver" in text:
            return (
                "Use a short reference path: a straight leg, a coordinated turn, "
                "and a speed change. Keep durations inside the short-sim budget."
            )
        if "tune" in text or "gain" in text:
            return (
                "If overshoot is high, lower the position gain. If the rise is "
                "slow and overshoot is already small, raise it slightly. Stay "
                "inside the gain clamps."
            )
        if "review" in text:
            return "Check estimator NEES, path error, the fault campaign, and the setpoint audit."
        return (
            "Break the goal into research, a concrete draft, and an independent "
            "review. Prefer evidence that can be checked without an external service."
        )


class OpenAICompatibleLLM:
    """Chat-completions client for any OpenAI-compatible base URL."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 3,
        backoff_base_s: float = 0.25,
        backoff_cap_s: float = 8.0,
        sleep=time.sleep,
        rng=None,
    ):
        self.model = model
        self.api_key = api_key
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.sleep = sleep
        self._rng = rng or __import__("random").Random(0)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            transport=transport,
        )

    def complete(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You assist an engineering orchestrator. Stay on simulation, "
                        "software, and bench tests. Do not propose motion for a "
                        "physical positioner."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response = self._client.post("/chat/completions", json=body, headers=headers)
            if response.status_code == 429:
                retry_after = _retry_after_header(response)
                last_error = RateLimitError("llm rate limited", retry_after=retry_after)
                if attempt == self.max_attempts:
                    break
                self.sleep(
                    retry_delay(
                        attempt,
                        self.backoff_base_s,
                        self.backoff_cap_s,
                        self._rng,
                        retry_after,
                    )
                )
                continue
            response.raise_for_status()
            payload = response.json()
            return payload["choices"][0]["message"]["content"]
        raise RateLimitError("llm rate limit persisted") from last_error

    def close(self) -> None:
        self._client.close()


def _retry_after_header(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def build_llm(settings: Settings) -> LLM:
    if settings.llm in {"", "mock"}:
        return MockLLM()
    if settings.llm in {"openai", "openai-compatible"}:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when SWARM_LLM=openai")
        return OpenAICompatibleLLM(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.openai_model,
            backoff_base_s=settings.backoff_base_s,
            backoff_cap_s=settings.backoff_cap_s,
        )
    raise RuntimeError(f"Unknown SWARM_LLM={settings.llm!r}")
