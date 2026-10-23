"""LangGraph coordinator.

The graph decides. Workers do the work. A step ends when the run is paused,
finished, failed, or waiting on the broker.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from swarm_orchestrator.guardrails import TERMINAL, deps_ready, note_fingerprint
from swarm_orchestrator.metrics import summarize
from swarm_orchestrator.planning import normalize_tasks


class GraphState(TypedDict, total=False):
    run_id: str
    goal: str
    team: str
    status: str
    phase: str
    error: str | None
    tasks: dict
    results: dict
    inflight: list
    inbox: list
    held_approvals: list
    pause: dict | None
    step: int
    max_steps: int
    spent: float
    budget: float
    plan_cost: float
    task_cost: float
    fingerprints: list
    confidence_threshold: float
    pending_events: list
    report: str | None
    metrics: dict
    plan_override: list | None
    created_at: float


def build_coordinator_graph():
    graph = StateGraph(GraphState)
    graph.add_node("route_in", lambda state: {})
    graph.add_node("decompose", node_decompose)
    graph.add_node("guard", node_guard)
    graph.add_node("dispatch", node_dispatch)
    graph.add_node("integrate", node_integrate)
    graph.add_node("pause", node_pause)
    graph.add_node("finalize", node_finalize)
    graph.add_node("fail", node_fail)
    graph.set_entry_point("route_in")
    graph.add_conditional_edges(
        "route_in",
        choose_entry,
        {"decompose": "decompose", "guard": "guard", "integrate": "integrate", "fail": "fail"},
    )
    graph.add_edge("decompose", "guard")
    graph.add_conditional_edges(
        "guard",
        after_guard,
        {"dispatch": "dispatch", "integrate": "integrate", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "dispatch",
        after_dispatch,
        {"await": END, "finalize": "finalize", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "integrate",
        after_integrate,
        {"guard": "guard", "pause": "pause", "finalize": "finalize", "fail": "fail"},
    )
    graph.add_edge("pause", END)
    graph.add_edge("finalize", END)
    graph.add_edge("fail", END)
    return graph.compile()


def choose_entry(state: GraphState) -> str:
    if state.get("status") == "failed":
        return "fail"
    if state.get("inbox") or state.get("held_approvals"):
        return "integrate"
    if not state.get("tasks"):
        return "decompose"
    return "guard"


def after_guard(state: GraphState) -> str:
    if state.get("status") == "failed":
        return "fail"
    if state.get("inbox") or state.get("held_approvals"):
        return "integrate"
    return "dispatch"


def after_dispatch(state: GraphState) -> str:
    if state.get("status") == "failed":
        return "fail"
    if state.get("phase") == "finalize":
        return "finalize"
    return "await"


def after_integrate(state: GraphState) -> str:
    if state.get("status") == "failed":
        return "fail"
    if state.get("status") == "paused":
        return "pause"
    if state.get("phase") == "finalize":
        return "finalize"
    return "guard"


def node_decompose(state: GraphState, runtime) -> dict:
    if state.get("tasks"):
        return {}
    ctx = runtime.context
    if state.get("plan_override"):
        templates = state["plan_override"]
    else:
        templates = ctx.planner.plan(state["goal"], state.get("team") or "generic")
    tasks = normalize_tasks(state["run_id"], templates, ctx.settings)
    events = list(state.get("pending_events") or [])
    events.append(
        _event(
            "planned",
            f"Planned {len(tasks)} tasks",
            {"tasks": [task["name"] for task in tasks.values()]},
        )
    )
    return {
        "tasks": tasks,
        "spent": float(state.get("spent", 0.0)) + float(state.get("plan_cost", 1.0)),
        "pending_events": events,
        "phase": "guard",
    }


def node_guard(state: GraphState, runtime) -> dict:
    del runtime
    step = int(state.get("step", 0)) + 1
    events = list(state.get("pending_events") or [])
    if step > int(state["max_steps"]):
        events.append(_event("run_failed", "Stopped: max steps", {"error": "max_steps"}))
        return {"step": step, "status": "failed", "error": "max_steps", "phase": "failed", "pending_events": events}
    if float(state.get("spent", 0.0)) > float(state["budget"]):
        events.append(_event("run_failed", "Stopped: budget", {"error": "budget"}))
        return {"step": step, "status": "failed", "error": "budget", "phase": "failed", "pending_events": events}
    snapshot = dict(state)
    snapshot["step"] = step
    loop = note_fingerprint(snapshot)
    if loop:
        events.append(_event("run_failed", "Stopped: repeated state", {"error": loop}))
        return {
            "step": step,
            "fingerprints": snapshot["fingerprints"],
            "status": "failed",
            "error": loop,
            "phase": "failed",
            "pending_events": events,
        }
    return {"step": step, "fingerprints": snapshot["fingerprints"], "phase": "guard", "pending_events": events}


def node_dispatch(state: GraphState, runtime) -> dict:
    ctx = runtime.context
    tasks = {key: dict(value) for key, value in state.get("tasks", {}).items()}
    for task in tasks.values():
        task["payload"] = dict(task.get("payload") or {})
    results = dict(state.get("results") or {})
    inflight = list(state.get("inflight") or [])
    events = list(state.get("pending_events") or [])
    _skip_blocked(tasks, results, inflight, events)
    ready = [
        task_id
        for task_id, task in tasks.items()
        if task_id not in results and task_id not in inflight and deps_ready(task, results)
    ]
    if not ready and not inflight:
        if tasks and all(task_id in results for task_id in tasks):
            return {"tasks": tasks, "results": results, "pending_events": events, "phase": "finalize"}
        events.append(_event("run_failed", "Stopped: deadlock", {"error": "deadlock"}))
        return {
            "tasks": tasks,
            "results": results,
            "pending_events": events,
            "status": "failed",
            "error": "deadlock",
            "phase": "failed",
        }
    if not ready:
        return {"tasks": tasks, "results": results, "inflight": inflight, "pending_events": events, "phase": "awaiting"}
    cost = sum(float(tasks[task_id].get("cost", state.get("task_cost", 1.0))) for task_id in ready)
    spent = float(state.get("spent", 0.0))
    if spent + cost > float(state["budget"]):
        events.append(_event("run_failed", "Stopped: budget", {"error": "budget"}))
        return {
            "tasks": tasks,
            "results": results,
            "pending_events": events,
            "status": "failed",
            "error": "budget",
            "phase": "failed",
            "spent": spent,
        }
    for task_id in ready:
        task = tasks[task_id]
        generation = int(task.get("generation", 0)) + 1
        task["generation"] = generation
        attempt = int(task.get("attempt", 1))
        payload = dict(task["payload"])
        payload["deps"] = {}
        for dep in task["deps"]:
            if dep in results:
                payload["deps"][tasks[dep]["name"]] = results[dep].get("output") or {}
        message = {
            "delivery_id": f"{task_id}:{generation}:{attempt}",
            "task_id": task_id,
            "run_id": state["run_id"],
            "name": task["name"],
            "agent": task["agent"],
            "attempt": attempt,
            "generation": generation,
            "max_attempts": int(task["max_attempts"]),
            "timeout_s": float(task["timeout_s"]),
            "risk": task.get("risk", "low"),
            "payload": payload,
        }
        ctx.bus.enqueue(message)
        inflight.append(task_id)
        events.append(_event("task_queued", f"Queued {task['name']}", {"task": task["name"], "agent": task["agent"]}))
    return {
        "tasks": tasks,
        "results": results,
        "inflight": inflight,
        "spent": spent + cost,
        "pending_events": events,
        "phase": "awaiting",
    }


def node_integrate(state: GraphState, runtime) -> dict:
    del runtime
    incoming = list(state.get("inbox") or []) + list(state.get("held_approvals") or [])
    results = dict(state.get("results") or {})
    inflight = list(state.get("inflight") or [])
    events = list(state.get("pending_events") or [])
    threshold = float(state.get("confidence_threshold", 0.75))
    approvals = []
    for item in incoming:
        kind = item.get("approval_kind")
        low = item.get("status") == "succeeded" and float(item.get("confidence") or 0.0) < threshold and not item.get("approved")
        if item.get("status") == "needs_approval" or kind in {"tool_commit", "low_confidence"} or low:
            approvals.append(item)
            continue
        _accept(results, inflight, events, item)
    if approvals:
        current = approvals[0]
        if current.get("approval_kind") == "low_confidence" or (
            current.get("status") == "succeeded" and float(current.get("confidence") or 1) < threshold
        ):
            pause_kind = "low_confidence"
        else:
            pause_kind = "tool_commit"
        pause = {
            "id": f"bp-{current['task_id']}-{int(state.get('step', 0))}",
            "task_id": current["task_id"],
            "kind": pause_kind,
            "reason": _pause_reason(pause_kind, current),
            "proposal": current.get("proposal"),
            "provisional": current,
        }
        if current["task_id"] in inflight:
            inflight.remove(current["task_id"])
        events.append(
            _event(
                "hitl_paused",
                pause["reason"],
                {"breakpoint_id": pause["id"], "task_id": current["task_id"], "kind": pause_kind},
            )
        )
        return {
            "results": results,
            "inflight": inflight,
            "inbox": [],
            "held_approvals": approvals[1:],
            "pause": pause,
            "status": "paused",
            "phase": "paused",
            "pending_events": events,
            "metrics": summarize(results),
        }
    phase = "guard"
    tasks = state.get("tasks") or {}
    if tasks and all(task_id in results for task_id in tasks):
        phase = "finalize"
    return {
        "results": results,
        "inflight": inflight,
        "inbox": [],
        "held_approvals": [],
        "pause": None,
        "status": "running",
        "phase": phase,
        "pending_events": events,
        "metrics": summarize(results),
    }


def node_pause(state: GraphState, runtime) -> dict:
    del runtime
    return {"status": "paused", "phase": "paused"}


def node_finalize(state: GraphState, runtime) -> dict:
    del runtime
    metrics = summarize(state.get("results") or {})
    report = state.get("report")
    for result in (state.get("results") or {}).values():
        output = result.get("output") or {}
        if output.get("report"):
            report = output["report"]
    events = list(state.get("pending_events") or [])
    hard_failure = any((result.get("status") in {"failed", "dlq"}) for result in (state.get("results") or {}).values())
    status = "failed" if hard_failure else "completed"
    events.append(
        _event(
            "run_completed" if status == "completed" else "run_failed",
            "Run finished" if status == "completed" else "Run finished with failures",
            {"error": state.get("error")},
        )
    )
    return {
        "status": status,
        "phase": status,
        "metrics": metrics,
        "report": report,
        "pending_events": events,
        "error": state.get("error") if status == "failed" else None,
    }


def node_fail(state: GraphState, runtime) -> dict:
    del runtime
    events = list(state.get("pending_events") or [])
    if not events or events[-1].get("type") != "run_failed":
        events.append(_event("run_failed", state.get("error") or "failed", {"error": state.get("error")}))
    return {
        "status": "failed",
        "phase": "failed",
        "metrics": summarize(state.get("results") or {}),
        "pending_events": events,
    }


def _skip_blocked(tasks: dict, results: dict, inflight: list[str], events: list) -> None:
    changed = True
    while changed:
        changed = False
        for task_id, task in tasks.items():
            if task_id in results or task_id in inflight:
                continue
            if task.get("dep_policy") == "best_effort":
                continue
            failed = [
                dep
                for dep in task["deps"]
                if results.get(dep, {}).get("status") in {"failed", "dlq", "rejected", "skipped"}
            ]
            if not failed:
                continue
            results[task_id] = {
                "task_id": task_id,
                "name": task["name"],
                "agent": task["agent"],
                "status": "skipped",
                "output": {},
                "confidence": 1.0,
                "error": "dependency did not succeed",
                "attempts": 0,
                "retries": 0,
                "latency_ms": 0.0,
                "risk": task.get("risk", "low"),
            }
            events.append(_event("task_skipped", f"Skipped {task['name']}", {"task": task["name"]}))
            changed = True


def _accept(results: dict, inflight: list[str], events: list, item: dict) -> None:
    results[item["task_id"]] = item
    if item["task_id"] in inflight:
        inflight.remove(item["task_id"])
    status = item.get("status")
    events.append(
        _event(
            f"task_{status}",
            f"{item.get('name') or item['task_id']} {status}",
            {"task": item.get("name"), "status": status, "retries": item.get("retries", 0)},
        )
    )


def _pause_reason(kind: str, item: dict) -> str:
    if kind == "tool_commit":
        proposal = item.get("proposal") or {}
        rejection = proposal.get("rejection")
        if rejection:
            return f"Approval required before a position command. The adapter will not send it: {rejection}"
        return "Approval required before a position or rate command is sent to the sim shell."
    return f"Confidence {item.get('confidence')} is below the threshold."


def _event(kind: str, message: str, data: dict | None = None) -> dict:
    return {"type": kind, "message": message, "data": data or {}}


# Re-exported for tests that want the terminal vocabulary.
__all__ = ["TERMINAL", "build_coordinator_graph"]
