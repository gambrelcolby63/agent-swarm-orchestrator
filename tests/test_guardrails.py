import random

import httpx
import pytest

from swarm_orchestrator.guardrails import fingerprint, note_fingerprint
from swarm_orchestrator.llm import OpenAICompatibleLLM
from swarm_orchestrator.retry import backoff_seconds, retry_delay


def test_backoff_grows_and_stays_inside_the_cap():
    rng = random.Random(1)
    first = [backoff_seconds(1, 0.1, 1.0, rng) for _ in range(20)]
    second = [backoff_seconds(3, 0.1, 1.0, rng) for _ in range(20)]
    assert min(first) >= 0.05
    assert max(first) <= 0.1
    assert min(second) >= 0.2
    assert max(second) <= 0.4
    capped = backoff_seconds(12, 0.1, 0.5, random.Random(2))
    assert 0.25 <= capped <= 0.5


def test_retry_after_is_honored():
    delay = retry_delay(1, 0.01, 0.2, random.Random(0), retry_after=1.5)
    assert delay == 1.5


def test_fingerprint_trips_on_a_repeated_state():
    state = {"phase": "guard", "results": {}, "inflight": [], "fingerprints": []}
    assert note_fingerprint(state, limit=3) is None
    assert note_fingerprint(state, limit=3) is None
    assert note_fingerprint(state, limit=3) == "loop_detected"
    assert fingerprint(state)


def test_openai_compatible_client_backs_off_on_429():
    calls = {"n": 0}
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatibleLLM(
        "http://llm.example/v1",
        "test-key",
        "mock-model",
        transport=httpx.MockTransport(handler),
        backoff_base_s=0.0,
        backoff_cap_s=0.0,
        sleep=sleeps.append,
    )
    assert client.complete("hello") == "ok"
    assert calls["n"] == 2
    assert sleeps == [0.0]
