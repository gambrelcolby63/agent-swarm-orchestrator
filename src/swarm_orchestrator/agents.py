"""Generic workers and the agent registry."""

from __future__ import annotations

import os
import time

from swarm_orchestrator.gimbal.team import (
    firmware_agent,
    gimbal_reviewer,
    scenario_agent,
    setpoint_agent,
    sim_runner_agent,
    tuning_agent,
)


def researcher(task: dict, ctx) -> dict:
    goal = task["payload"].get("goal", "")
    notes = ctx.llm.complete(f"RESEARCH\n{goal}")
    return {"status": "succeeded", "confidence": 0.9, "output": {"notes": notes}}


def coder(task: dict, ctx) -> dict:
    notes = task["payload"]["deps"]["research"]["notes"]
    return {
        "status": "succeeded",
        "confidence": 0.9,
        "output": {
            "draft": notes,
            "steps": [
                "Restate the goal as a checkable claim.",
                "Keep the change inside the orchestrator and its tests.",
                "Hand the draft to the reviewer.",
            ],
        },
    }


def reviewer(task: dict, ctx) -> dict:
    goal = task["payload"].get("goal", "")
    narrative = ctx.llm.complete(f"REVIEW\n{goal}")
    confidence = 0.42 if "low-confidence" in goal.lower() else 0.93
    return {
        "status": "succeeded",
        "confidence": confidence,
        "output": {"narrative": narrative, "draft": task["payload"]["deps"].get("draft")},
    }


def probe(task: dict, ctx) -> dict:
    ctx.bus.incr(f"swarm:probe-runs:{task['id']}")
    ctx.bus.r.set(f"swarm:started:{task['id']}", "1")
    delay = float(task["payload"].get("sleep_s") or 0.0)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if ctx.cancel.is_set():
            return {"status": "failed", "confidence": 0.0, "error": "cancelled", "output": {}}
        time.sleep(0.01)
    if os.environ.get("SWARM_CRASH") == "1":
        os._exit(137)
    return {"status": "succeeded", "confidence": 0.99, "output": {"ok": True}}


def build_registry() -> dict:
    return {
        "researcher": researcher,
        "coder": coder,
        "reviewer": reviewer,
        "scenario": scenario_agent,
        "sim_runner": sim_runner_agent,
        "firmware": firmware_agent,
        "tuning": tuning_agent,
        "setpoint": setpoint_agent,
        "gimbal_reviewer": gimbal_reviewer,
        "probe": probe,
    }
