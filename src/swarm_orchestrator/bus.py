"""Redis Streams backplane.

Tasks stay pending until a worker acknowledges them, so a crash mid-task is a
reclaim, not a lost message. Results use a set-once key so a redelivery cannot
apply the same task twice.
"""

from __future__ import annotations

import json
import time
from typing import Any

import redis

TASK_STREAM = "swarm:tasks"
DLQ_STREAM = "swarm:dlq"
GROUP = "workers"


def _loads(raw: str) -> dict:
    return json.loads(raw)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class RedisBus:
    def __init__(self, client: redis.Redis):
        self.r = client
        self.ensure_group()

    @classmethod
    def connect(cls, url: str) -> RedisBus:
        return cls(redis.Redis.from_url(url, decode_responses=True))

    def ensure_group(self) -> None:
        try:
            self.r.xgroup_create(TASK_STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, message: dict) -> bool:
        """Add a task delivery. The same delivery id is ignored."""

        delivery_id = message["delivery_id"]
        if not self.r.set(f"swarm:delivery:{delivery_id}", "1", nx=True):
            return False
        self.r.xadd(TASK_STREAM, {"data": _dumps(message)})
        return True

    def read(self, consumer: str, count: int = 10, block_ms: int = 20) -> list[dict]:
        try:
            rows = self.r.xreadgroup(
                GROUP,
                consumer,
                {TASK_STREAM: ">"},
                count=count,
                block=block_ms,
            )
        except redis.ResponseError:
            rows = self.r.xreadgroup(GROUP, consumer, {TASK_STREAM: ">"}, count=count)
        return _deliveries(rows)

    def reclaim(self, consumer: str, min_idle_ms: int, count: int = 10) -> list[dict]:
        try:
            claimed = self.r.xautoclaim(
                TASK_STREAM,
                GROUP,
                consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
            messages = _autoclaim_messages(claimed)
            return [_delivery(mid, fields) for mid, fields in messages if fields]
        except (redis.ResponseError, AttributeError):
            return self._reclaim_via_claim(consumer, min_idle_ms, count)

    def _reclaim_via_claim(self, consumer: str, min_idle_ms: int, count: int) -> list[dict]:
        pending = self.r.xpending_range(TASK_STREAM, GROUP, min="-", max="+", count=count)
        ids = []
        for item in pending:
            idle = item.get("time_since_delivered", 0) if isinstance(item, dict) else item["time_since_delivered"]
            if idle >= min_idle_ms:
                ids.append(item["message_id"] if isinstance(item, dict) else item["message_id"])
        if not ids:
            return []
        claimed = self.r.xclaim(TASK_STREAM, GROUP, consumer, min_idle_time=min_idle_ms, message_ids=ids)
        out = []
        for mid, fields in claimed:
            if fields:
                out.append(_delivery(mid, fields))
        return out

    def ack(self, broker_id: str) -> None:
        self.r.xack(TASK_STREAM, GROUP, broker_id)

    def publish_result(self, run_id: str, result: dict) -> bool:
        """Store a terminal result once and push it to the run inbox.

        A crash between the store and the inbox push is repaired on the next
        delivery: the result is already there, and the inbox flag is what gets
        retried.
        """

        task_id = result["task_id"]
        key = f"swarm:result:{task_id}"
        encoded = _dumps(result)
        created = bool(self.r.set(key, encoded, nx=True))
        if not created:
            encoded = self.r.get(key) or encoded
        if self.r.set(f"swarm:inboxed:{task_id}", "1", nx=True):
            self.r.rpush(f"swarm:inbox:{run_id}", encoded)
            return True
        return False

    def get_result(self, task_id: str) -> dict | None:
        raw = self.r.get(f"swarm:result:{task_id}")
        return _loads(raw) if raw else None

    def stage_approval(self, run_id: str, result: dict) -> bool:
        """Inbox a human-approval request without marking the task terminal."""

        task_id = result["task_id"]
        key = f"swarm:proposal:{task_id}"
        encoded = _dumps(result)
        created = bool(self.r.set(key, encoded, nx=True))
        if not created:
            encoded = self.r.get(key) or encoded
        if self.r.set(f"swarm:proposal-inboxed:{task_id}", "1", nx=True):
            self.r.rpush(f"swarm:inbox:{run_id}", encoded)
            return True
        return False

    def has_staged(self, task_id: str) -> bool:
        return bool(self.r.get(f"swarm:proposal:{task_id}"))

    def get_proposal(self, task_id: str) -> dict | None:
        raw = self.r.get(f"swarm:proposal:{task_id}")
        return _loads(raw) if raw else None

    def clear_stage(self, task_id: str) -> None:
        self.r.delete(f"swarm:proposal:{task_id}", f"swarm:proposal-inboxed:{task_id}")

    def pop_inbox(self, run_id: str) -> list[dict]:
        key = f"swarm:inbox:{run_id}"
        items: list[dict] = []
        while True:
            raw = self.r.lpop(key)
            if raw is None:
                return items
            items.append(_loads(raw))

    def add_dlq(self, record: dict) -> str:
        return str(self.r.xadd(DLQ_STREAM, {"data": _dumps(record)}))

    def dlq_records(self) -> list[dict]:
        rows = self.r.xrange(DLQ_STREAM, min="-", max="+")
        return [_loads(fields["data"]) for _mid, fields in rows]

    def add_event(self, run_id: str, event: dict) -> dict:
        seq = int(self.r.incr(f"swarm:eventseq:{run_id}"))
        stored = dict(event)
        stored["seq"] = seq
        stored.setdefault("ts", time.time())
        stored["run_id"] = run_id
        self.r.xadd(f"swarm:events:{run_id}", {"data": _dumps(stored)})
        return stored

    def events_after(self, run_id: str, seq: int) -> list[dict]:
        rows = self.r.xrange(f"swarm:events:{run_id}", min="-", max="+")
        events = [_loads(fields["data"]) for _mid, fields in rows]
        return [event for event in events if event["seq"] > seq]

    def save_checkpoint(self, run_id: str, state: dict) -> None:
        self.r.set(f"swarm:checkpoint:{run_id}", _dumps(state))
        self.r.hset("swarm:runs", run_id, state.get("status", ""))

    def load_checkpoint(self, run_id: str) -> dict | None:
        raw = self.r.get(f"swarm:checkpoint:{run_id}")
        return _loads(raw) if raw else None

    def list_runs(self) -> list[tuple[str, str]]:
        rows = self.r.hgetall("swarm:runs")
        return sorted(rows.items())

    def acquire_lock(self, task_id: str, owner: str, ttl_ms: int) -> bool:
        return bool(self.r.set(f"swarm:lock:{task_id}", owner, nx=True, px=ttl_ms))

    def release_lock(self, task_id: str, owner: str) -> None:
        key = f"swarm:lock:{task_id}"
        if self.r.get(key) == owner:
            self.r.delete(key)

    def incr(self, key: str) -> int:
        return int(self.r.incr(key))


def _deliveries(rows: Any) -> list[dict]:
    if not rows:
        return []
    out = []
    for _stream, messages in rows:
        for broker_id, fields in messages:
            if not fields:
                continue
            out.append(_delivery(broker_id, fields))
    return out


def _delivery(broker_id: str, fields: dict) -> dict:
    message = _loads(fields["data"])
    message["broker_id"] = broker_id
    return message


def _autoclaim_messages(claimed: Any) -> list:
    if isinstance(claimed, (list, tuple)):
        if len(claimed) >= 2 and isinstance(claimed[1], list):
            return claimed[1]
    return []
