import fakeredis

from swarm_orchestrator.engine import Engine
from swarm_orchestrator.settings import Settings


def make_engine(**overrides) -> Engine:
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    values = dict(
        embedded_workers=True,
        llm="mock",
        gimbal_backend="short",
        uart_mode="sim",
        task_timeout_s=3,
        max_attempts=3,
        backoff_base_s=0.0,
        backoff_cap_s=0.0,
        visibility_ms=30,
        max_steps=24,
        budget=40,
        short_mc_runs=3,
        allow_chaos=False,
    )
    values.update(overrides)
    return Engine.create(Settings(**values), client=client)
