"""Worker loop. A message stays pending until it is acknowledged."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from swarm_orchestrator.retry import CrashBeforeAck, RateLimitError, RetryableError, TaskTimeout, retry_delay


class WorkerRunner:
    def __init__(self, ctx, consumer: str | None = None):
        self.ctx = ctx
        self.consumer = consumer or f"worker-{os.getpid()}"
        self.sleep = time.sleep

    def handle(self, message: dict) -> None:
        bus = self.ctx.bus
        task_id = message["task_id"]
        existing = bus.get_result(task_id)
        if existing:
            bus.publish_result(message["run_id"], existing)
            bus.ack(message["broker_id"])
            return
        if bus.has_staged(task_id) and not (message.get("payload") or {}).get("approval_token"):
            bus.ack(message["broker_id"])
            return
        ttl = max(1000, int(float(message["timeout_s"]) * 1000) + self.ctx.settings.visibility_ms)
        if not bus.acquire_lock(task_id, self.consumer, ttl):
            return
        try:
            self._execute(message)
        except CrashBeforeAck:
            return
        except RetryableError as exc:
            self._retry_or_dlq(message, exc)
        finally:
            bus.release_lock(task_id, self.consumer)

    def drain(self, run_id: str, inflight: list[str], timeout_s: float) -> None:
        pending = set(inflight)
        deadline = time.monotonic() + timeout_s
        while pending and time.monotonic() < deadline:
            for task_id in list(pending):
                if self._settled(task_id):
                    pending.discard(task_id)
            if not pending:
                return
            messages = self.ctx.bus.read(self.consumer, block_ms=20)
            if not messages:
                messages = self.ctx.bus.reclaim(self.consumer, self.ctx.settings.visibility_ms)
            if not messages:
                self.sleep(0.01)
                continue
            for message in messages:
                self.handle(message)
        return

    def serve_forever(self) -> None:
        while True:
            messages = self.ctx.bus.read(self.consumer, block_ms=1000)
            if not messages:
                messages = self.ctx.bus.reclaim(self.consumer, self.ctx.settings.visibility_ms)
            for message in messages:
                self.handle(message)

    def _settled(self, task_id: str) -> bool:
        return self.ctx.bus.get_result(task_id) is not None or self.ctx.bus.has_staged(task_id)

    def _execute(self, message: dict) -> None:
        bus = self.ctx.bus
        redeliveries = bus.incr(f"swarm:deliveries:{message['delivery_id']}")
        self._maybe_chaos(message)
        started = time.perf_counter()
        try:
            output = self._call_agent(message)
        except (RetryableError, CrashBeforeAck):
            raise
        except Exception as exc:
            raise RetryableError(str(exc)) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        result = self._pack(message, output, latency_ms, redeliveries)
        if self.ctx.settings.allow_chaos and (message.get("payload") or {}).get("_chaos") == "crash_after_result":
            seen = bus.incr(f"swarm:chaos-after:{message['task_id']}")
            self._publish(message, result)
            if seen == 1:
                raise CrashBeforeAck()
            bus.ack(message["broker_id"])
            return
        self._publish(message, result)
        bus.ack(message["broker_id"])

    def _maybe_chaos(self, message: dict) -> None:
        if not self.ctx.settings.allow_chaos:
            return
        payload = message.get("payload") or {}
        kind = payload.get("_chaos")
        if not kind or kind == "crash_after_result":
            return
        seen = self.ctx.bus.incr(f"swarm:chaos:{message['task_id']}")
        if kind == "crash_once" and seen == 1:
            raise CrashBeforeAck()
        if kind == "rate_limit" and seen <= int(payload.get("_chaos_times", 2)):
            raise RateLimitError("injected 429", retry_after=float(payload.get("_retry_after", 0) or 0))
        if kind == "timeout_once" and seen == 1:
            raise TaskTimeout("injected timeout")
        if kind == "always_fail":
            raise RetryableError("injected failure")

    def _call_agent(self, message: dict):
        import threading

        from swarm_orchestrator.runtime import AgentContext

        agent = self.ctx.registry[message["agent"]]
        cancel = threading.Event()
        agent_ctx = AgentContext(
            llm=self.ctx.llm,
            settings=self.ctx.settings,
            bus=self.ctx.bus,
            uart=self.ctx.uart,
            cancel=cancel,
        )
        task = {
            "id": message["task_id"],
            "name": message["name"],
            "agent": message["agent"],
            "payload": message.get("payload") or {},
        }
        timeout = float(message["timeout_s"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent, task, agent_ctx)
            try:
                output = future.result(timeout=timeout)
            except FuturesTimeout as exc:
                cancel.set()
                raise TaskTimeout(f"exceeded {timeout}s") from exc
        confidence = float(output.get("confidence", 1.0))
        if output.get("status", "succeeded") == "succeeded" and confidence < self.ctx.settings.confidence_threshold:
            output = dict(output)
            output["status"] = "needs_approval"
            output["approval_kind"] = "low_confidence"
        elif output.get("status") == "needs_approval":
            output = dict(output)
            output["approval_kind"] = output.get("approval_kind") or "tool_commit"
        return output

    def _publish(self, message: dict, result: dict) -> None:
        if result.get("status") == "needs_approval":
            self.ctx.bus.stage_approval(message["run_id"], result)
        else:
            self.ctx.bus.publish_result(message["run_id"], result)

    def _retry_or_dlq(self, message: dict, exc: Exception) -> None:
        attempt = int(message["attempt"])
        if attempt >= int(message["max_attempts"]):
            self._dlq(message, str(exc))
            self.ctx.bus.ack(message["broker_id"])
            return
        retry_after = getattr(exc, "retry_after", None)
        delay = retry_delay(
            attempt,
            self.ctx.settings.backoff_base_s,
            self.ctx.settings.backoff_cap_s,
            self.ctx.rng,
            retry_after,
        )
        self.sleep(delay)
        follow = {key: value for key, value in message.items() if key != "broker_id"}
        follow["attempt"] = attempt + 1
        follow["delivery_id"] = f"{message['task_id']}:{message['generation']}:{follow['attempt']}"
        self.ctx.bus.enqueue(follow)
        self.ctx.bus.ack(message["broker_id"])

    def _dlq(self, message: dict, error: str) -> None:
        record = {
            "task_id": message["task_id"],
            "run_id": message["run_id"],
            "agent": message["agent"],
            "name": message.get("name"),
            "attempts": int(message["attempt"]),
            "error": error,
            "ts": time.time(),
        }
        self.ctx.bus.add_dlq(record)
        result = self._pack(
            message,
            {"status": "dlq", "confidence": 0.0, "output": {"dlq": record}, "error": error},
            0.0,
            1,
        )
        result["attempts"] = int(message["attempt"])
        result["retries"] = max(0, int(message["attempt"]) - 1)
        self.ctx.bus.publish_result(message["run_id"], result)

    def _pack(self, message: dict, output: dict, latency_ms: float, redeliveries: int) -> dict:
        attempts = int(message["attempt"])
        retries = max(0, attempts - 1) + max(0, int(redeliveries) - 1)
        return {
            "task_id": message["task_id"],
            "name": message.get("name"),
            "agent": message.get("agent"),
            "status": output.get("status", "succeeded"),
            "output": output.get("output") or {},
            "confidence": float(output.get("confidence", 0.0)),
            "error": output.get("error"),
            "proposal": output.get("proposal"),
            "approval_kind": output.get("approval_kind"),
            "attempts": attempts,
            "retries": retries,
            "latency_ms": round(latency_ms, 3),
            "risk": message.get("risk", "low"),
        }


def main() -> None:
    from swarm_orchestrator.runtime import build_context
    from swarm_orchestrator.settings import Settings

    settings = Settings.from_env()
    WorkerRunner(build_context(settings)).serve_forever()


if __name__ == "__main__":
    main()
