"""Deterministic demo of the pan/tilt engineering team."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fakeredis

from swarm_orchestrator.engine import Engine
from swarm_orchestrator.metrics import dag_mermaid
from swarm_orchestrator.settings import Settings

GOAL = (
    "Run a short pan/tilt engineering check: maneuver profile, estimator consistency, "
    "firmware fault campaign, a gain proposal, and one simulated setpoint inside the soft limits."
)


def run_demo(output: Path, team: str = "gimbal", approve: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    settings = Settings(
        embedded_workers=True,
        llm="mock",
        gimbal_backend="short",
        uart_mode="sim",
        task_timeout_s=8,
        backoff_base_s=0.0,
        backoff_cap_s=0.0,
        visibility_ms=20,
        short_mc_runs=4,
    )
    engine = Engine.create(settings, client=client)
    run_id = "demo-gimbal" if team == "gimbal" else "demo-generic"
    state = engine.create_run(GOAL if team == "gimbal" else "Draft a test plan for the orchestrator.", team=team, run_id=run_id)
    state = engine.pump(run_id)
    decisions = []
    while state["status"] == "paused" and approve:
        breakpoint_id = state["pause"]["id"]
        decisions.append(
            {
                "breakpoint_id": breakpoint_id,
                "kind": state["pause"]["kind"],
                "reason": state["pause"]["reason"],
                "lines": ((state["pause"].get("proposal") or {}).get("lines")),
                "decision": "approve",
            }
        )
        engine.decide(run_id, breakpoint_id, "approve", note="demo approval")
        state = engine.pump(run_id)
    events = engine.bus.events_after(run_id, 0)
    trace = {
        "run_id": run_id,
        "team": team,
        "status": state["status"],
        "error": state.get("error"),
        "decisions": decisions,
        "tasks": [
            {
                "name": result.get("name"),
                "agent": result.get("agent"),
                "status": result.get("status"),
                "retries": result.get("retries"),
                "latency_ms": result.get("latency_ms"),
                "confidence": result.get("confidence"),
            }
            for result in (state.get("results") or {}).values()
        ],
        "metrics": _round(state.get("metrics") or {}),
        "events": [{"type": event["type"], "message": event["message"]} for event in events],
        "shell": list(engine.ctx.uart.transport.sent),
    }
    paused = (state.get("pause") or {}).get("task_id")
    mermaid = dag_mermaid(state.get("tasks") or {}, state.get("results") or {}, state.get("inflight"), paused)
    (output / "gimbal_trace.json").write_text(json.dumps(trace, indent=2) + "\n")
    (output / "gimbal_dag.md").write_text("```mermaid\n" + mermaid + "\n```\n")
    report = state.get("report") or "No report."
    (output / "gimbal_report.md").write_text(report + "\n")
    return trace


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mock-LLM pan/tilt demo and write a trace.")
    parser.add_argument("--output", type=Path, default=Path("docs/samples"))
    parser.add_argument("--team", choices=["gimbal", "generic"], default="gimbal")
    parser.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    trace = run_demo(args.output, team=args.team, approve=not args.reject)
    print(json.dumps({"status": trace["status"], "metrics": trace["metrics"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
