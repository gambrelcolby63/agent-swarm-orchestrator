"""Run engine. The graph is the policy; Redis is the memory that survives a restart."""

from __future__ import annotations

import time
import uuid

from swarm_orchestrator.graph import build_coordinator_graph
from swarm_orchestrator.runtime import AppContext, build_context
from swarm_orchestrator.settings import Settings
from swarm_orchestrator.worker import WorkerRunner


class Engine:
    def __init__(self, ctx: AppContext, graph=None, runner: WorkerRunner | None = None):
        self.ctx = ctx
        self.bus = ctx.bus
        self.settings = ctx.settings
        self.graph = graph or build_coordinator_graph()
        embedded = ctx.settings.embedded_workers
        self.runner = runner if runner is not None else (WorkerRunner(ctx) if embedded else None)

    @classmethod
    def create(cls, settings: Settings | None = None, client=None, uart=None) -> Engine:
        settings = settings or Settings.from_env()
        return cls(build_context(settings, client=client, uart=uart))

    def create_run(
        self,
        goal: str,
        team: str = "generic",
        plan: list | None = None,
        run_id: str | None = None,
    ) -> dict:
        run_id = run_id or uuid.uuid4().hex[:12]
        state = initial_state(run_id, goal, team, self.settings, plan)
        self.bus.save_checkpoint(run_id, state)
        self.bus.add_event(
            run_id,
            {"type": "run_started", "message": "Run started", "data": {"team": team, "goal": goal}},
        )
        return state

    def pump(self, run_id: str) -> dict:
        state = self._load(run_id)
        if state["status"] in {"completed", "failed", "paused"}:
            return state
        for _ in range(self.settings.max_loops):
            if state.get("phase") != "awaiting":
                state = self.graph.invoke(state, context=self.ctx)
                state = self._flush(run_id, state)
                self.bus.save_checkpoint(run_id, state)
                if state["status"] in {"completed", "failed", "paused"}:
                    return state
            if state.get("phase") == "awaiting":
                timeout = self._wait_budget(state)
                self._collect(run_id, state, timeout)
                inbox = self._gather_inbox(state)
                state = dict(state)
                state["inbox"] = inbox
                state["phase"] = "integrate"
                self.bus.save_checkpoint(run_id, state)
                continue
            state = dict(state)
            state["status"] = "failed"
            state["error"] = f"unexpected phase {state.get('phase')}"
            self.bus.save_checkpoint(run_id, state)
            return state
        state = dict(state)
        state["status"] = "failed"
        state["error"] = "max_loops"
        self._flush(run_id, state)
        self.bus.save_checkpoint(run_id, state)
        return state

    def decide(self, run_id: str, breakpoint_id: str, decision: str, note: str = "") -> dict:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        state = self._load(run_id)
        pause = state.get("pause")
        if state.get("status") != "paused" or not pause or pause.get("id") != breakpoint_id:
            raise KeyError("breakpoint is not active")
        task_id = pause["task_id"]
        self.bus.clear_stage(task_id)
        task = state["tasks"][task_id]
        state = dict(state)
        state["tasks"] = {key: dict(value) for key, value in state["tasks"].items()}
        state["tasks"][task_id] = dict(task)
        state["tasks"][task_id]["payload"] = dict(task.get("payload") or {})
        state["results"] = dict(state.get("results") or {})
        if decision == "approve" and pause["kind"] == "tool_commit":
            proposal = pause.get("proposal") or {}
            state["tasks"][task_id]["payload"]["approval_token"] = proposal.get("token")
            state["tasks"][task_id]["payload"]["proposal"] = proposal
        elif decision == "approve":
            accepted = dict(pause.get("provisional") or {})
            accepted["status"] = "succeeded"
            accepted["approved"] = True
            accepted["approval_kind"] = None
            output = dict(accepted.get("output") or {})
            output["operator_note"] = note
            accepted["output"] = output
            state["results"][task_id] = accepted
            self.bus.publish_result(run_id, accepted)
        else:
            rejected = {
                "task_id": task_id,
                "name": task["name"],
                "agent": task["agent"],
                "status": "rejected",
                "output": {"note": note},
                "confidence": (pause.get("provisional") or {}).get("confidence", 0.0),
                "error": note or "rejected by operator",
                "attempts": (pause.get("provisional") or {}).get("attempts", 0),
                "retries": (pause.get("provisional") or {}).get("retries", 0),
                "latency_ms": (pause.get("provisional") or {}).get("latency_ms", 0.0),
                "risk": task.get("risk", "low"),
            }
            state["results"][task_id] = rejected
            self.bus.publish_result(run_id, rejected)
        inflight = [item for item in state.get("inflight") or [] if item != task_id]
        state["inflight"] = inflight
        state["pause"] = None
        state["status"] = "running"
        state["phase"] = "guard"
        self.bus.add_event(
            run_id,
            {
                "type": f"hitl_{decision}d",
                "message": f"Operator {decision}d the breakpoint",
                "data": {"breakpoint_id": breakpoint_id, "task_id": task_id, "note": note},
            },
        )
        self.bus.save_checkpoint(run_id, state)
        return state

    def _collect(self, run_id: str, state: dict, timeout_s: float) -> None:
        inflight = list(state.get("inflight") or [])
        if self.runner is not None:
            self.runner.drain(run_id, inflight, timeout_s)
            return
        deadline = time.monotonic() + timeout_s
        pending = set(inflight)
        while pending and time.monotonic() < deadline:
            for task_id in list(pending):
                if self.bus.get_result(task_id) or self.bus.has_staged(task_id):
                    pending.discard(task_id)
            if pending:
                time.sleep(0.02)

    def _gather_inbox(self, state: dict) -> list[dict]:
        inbox = self.bus.pop_inbox(state["run_id"])
        seen = {item["task_id"] for item in inbox}
        for task_id in list(state.get("inflight") or []):
            if task_id in seen or task_id in (state.get("results") or {}):
                continue
            staged = self.bus.get_proposal(task_id)
            if staged is not None:
                inbox.append(staged)
                seen.add(task_id)
                continue
            result = self.bus.get_result(task_id)
            if result is not None:
                inbox.append(result)
                seen.add(task_id)
                continue
            inbox.append(self._timeout_result(state, task_id))
        return inbox

    def _timeout_result(self, state: dict, task_id: str) -> dict:
        task = state["tasks"][task_id]
        error = "timed out waiting for a worker"
        record = {
            "task_id": task_id,
            "run_id": state["run_id"],
            "agent": task["agent"],
            "name": task["name"],
            "attempts": int(task.get("max_attempts", 1)),
            "error": error,
            "ts": time.time(),
        }
        self.bus.add_dlq(record)
        result = {
            "task_id": task_id,
            "name": task["name"],
            "agent": task["agent"],
            "status": "dlq",
            "output": {"dlq": record},
            "confidence": 0.0,
            "error": error,
            "attempts": int(task.get("max_attempts", 1)),
            "retries": max(0, int(task.get("max_attempts", 1)) - 1),
            "latency_ms": float(task.get("timeout_s", 0.0)) * 1000.0,
            "risk": task.get("risk", "low"),
        }
        stored = self.bus.get_result(task_id)
        if stored:
            return stored
        self.bus.publish_result(state["run_id"], result)
        return self.bus.get_result(task_id) or result

    def _wait_budget(self, state: dict) -> float:
        inflight = state.get("inflight") or []
        if not inflight:
            return 1.0
        per_task = max(float(state["tasks"][task_id]["timeout_s"]) for task_id in inflight)
        attempts = max(int(state["tasks"][task_id]["max_attempts"]) for task_id in inflight)
        # Retries sleep inside the worker, so the drain window covers every attempt.
        return per_task * attempts + self.settings.backoff_cap_s * attempts + 1.0

    def _flush(self, run_id: str, state: dict) -> dict:
        state = dict(state)
        pending = list(state.get("pending_events") or [])
        state["pending_events"] = []
        for event in pending:
            self.bus.add_event(run_id, event)
        return state

    def _load(self, run_id: str) -> dict:
        state = self.bus.load_checkpoint(run_id)
        if state is None:
            raise KeyError(run_id)
        return state


def initial_state(run_id: str, goal: str, team: str, settings: Settings, plan: list | None) -> dict:
    return {
        "run_id": run_id,
        "goal": goal,
        "team": team,
        "status": "running",
        "phase": "decompose",
        "error": None,
        "tasks": {},
        "results": {},
        "inflight": [],
        "inbox": [],
        "held_approvals": [],
        "pause": None,
        "step": 0,
        "max_steps": settings.max_steps,
        "spent": 0.0,
        "budget": settings.budget,
        "plan_cost": settings.plan_cost,
        "task_cost": settings.task_cost,
        "fingerprints": [],
        "confidence_threshold": settings.confidence_threshold,
        "pending_events": [],
        "report": None,
        "metrics": {},
        "plan_override": plan,
        "created_at": time.time(),
    }
