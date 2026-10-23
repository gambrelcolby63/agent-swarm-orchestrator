import os
import subprocess
import sys
import time

import pytest
import redis

from tests.conftest import make_engine


def _result(state):
    return next(iter(state["results"].values()))


def test_worker_crash_before_ack_is_retried_once():
    engine = make_engine(allow_chaos=True, visibility_ms=20)
    plan = [{"name": "fragile", "agent": "probe", "deps": [], "payload": {"_chaos": "crash_once"}}]
    state = engine.create_run("crash", plan=plan)
    done = engine.pump(state["run_id"])
    result = _result(done)
    assert done["status"] == "completed"
    assert result["status"] == "succeeded"
    assert result["retries"] >= 1
    runs = int(engine.bus.r.get(f"swarm:probe-runs:{result['task_id']}"))
    assert runs == 1


def test_crash_after_result_does_not_run_the_agent_twice():
    engine = make_engine(allow_chaos=True, visibility_ms=15)
    plan = [{"name": "fragile", "agent": "probe", "deps": [], "payload": {"_chaos": "crash_after_result"}}]
    state = engine.create_run("after", plan=plan)
    done = engine.pump(state["run_id"])
    result = _result(done)
    assert result["status"] == "succeeded"
    # The unacked delivery is still pending. Reclaiming it must not repeat the work.
    time.sleep(0.05)
    pending = engine.bus.reclaim(engine.runner.consumer, 0)
    for message in pending:
        engine.runner.handle(message)
    runs = int(engine.bus.r.get(f"swarm:probe-runs:{result['task_id']}"))
    assert runs == 1
    assert engine.bus.get_result(result["task_id"])["status"] == "succeeded"


def test_rate_limit_retries_then_succeeds():
    engine = make_engine(allow_chaos=True, max_attempts=4)
    plan = [
        {
            "name": "limited",
            "agent": "probe",
            "deps": [],
            "max_attempts": 4,
            "payload": {"_chaos": "rate_limit", "_chaos_times": 2, "_retry_after": 0},
        }
    ]
    state = engine.create_run("429", plan=plan)
    done = engine.pump(state["run_id"])
    result = _result(done)
    assert result["status"] == "succeeded"
    assert result["attempts"] == 3
    assert result["retries"] == 2
    assert done["metrics"]["retries"] == 2


def test_exhausted_retries_land_in_the_dlq():
    engine = make_engine(allow_chaos=True, max_attempts=3)
    plan = [
        {
            "name": "doomed",
            "agent": "probe",
            "deps": [],
            "max_attempts": 3,
            "payload": {"_chaos": "always_fail"},
        }
    ]
    state = engine.create_run("dlq", plan=plan)
    done = engine.pump(state["run_id"])
    assert _result(done)["status"] == "dlq"
    records = engine.bus.dlq_records()
    assert len(records) == 1
    assert records[0]["task_id"].endswith(":doomed")
    assert done["status"] == "failed"


def test_duplicate_delivery_id_is_ignored():
    engine = make_engine()
    message = {
        "delivery_id": "same",
        "task_id": "t",
        "run_id": "r",
        "name": "n",
        "agent": "probe",
        "attempt": 1,
        "generation": 1,
        "max_attempts": 1,
        "timeout_s": 1,
        "risk": "low",
        "payload": {},
    }
    assert engine.bus.enqueue(message) is True
    assert engine.bus.enqueue(message) is False


def _redis_url():
    return os.environ.get("REDIS_URL", "")


def test_sigkill_mid_task_is_reclaimed(tmp_path):
    url = _redis_url()
    if not url:
        pytest.skip("REDIS_URL is not set")
    try:
        client = redis.Redis.from_url(url.rsplit("/", 1)[0] + "/15", decode_responses=True)
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis is not reachable")
    client.flushdb()
    from swarm_orchestrator.engine import Engine
    from swarm_orchestrator.settings import Settings

    settings = Settings(
        redis_url=url.rsplit("/", 1)[0] + "/15",
        embedded_workers=False,
        visibility_ms=100,
        task_timeout_s=0.3,
        max_attempts=2,
        backoff_base_s=0,
        backoff_cap_s=3,
        llm="mock",
    )
    engine = Engine.create(settings, client=client)
    plan = [{"name": "fragile", "agent": "probe", "deps": [], "timeout_s": 0.4, "payload": {}}]
    state = engine.create_run("kill", plan=plan)
    env = os.environ.copy()
    env["REDIS_URL"] = settings.redis_url
    env["SWARM_VISIBILITY_MS"] = "200"
    env["SWARM_TASK_TIMEOUT_S"] = "0.4"
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), *sys.path])
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    crashed = env.copy()
    crashed["SWARM_CRASH"] = "1"
    first = subprocess.Popen([sys.executable, "-m", "swarm_orchestrator.worker"], env=crashed)
    import threading

    holder = {}

    def _pump():
        holder["state"] = engine.pump(state["run_id"])

    thread = threading.Thread(target=_pump)
    thread.start()
    first.wait(timeout=10)
    assert first.returncode == 137
    second = subprocess.Popen([sys.executable, "-m", "swarm_orchestrator.worker"], env=env)
    thread.join(timeout=15)
    second.terminate()
    second.wait(timeout=5)
    assert "state" in holder
    done = holder["state"]
    result = _result(done)
    assert result["status"] == "succeeded"
    runs = int(client.get(f"swarm:probe-runs:{result['task_id']}"))
    assert runs >= 1
