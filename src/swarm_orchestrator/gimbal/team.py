"""Pan/tilt engineering team: scenario, sim, firmware, tuning, setpoint, review."""

from __future__ import annotations

from swarm_orchestrator.gimbal.firmware import run_campaign, try_host_tests, try_renode_tests
from swarm_orchestrator.gimbal.native_sim import run_native
from swarm_orchestrator.gimbal.scenario import build_scenario, dump_scenario, parse_setpoint
from swarm_orchestrator.gimbal.short_sim import run_short
from swarm_orchestrator.gimbal.tuning import evaluate, propose_gains
from swarm_orchestrator.gimbal.uart import ApprovalRequired, LimitRejected, ToolDisabled


def gimbal_templates(goal: str, note: str, settings) -> list[dict]:
    pan, tilt, kind = parse_setpoint(goal)
    short = settings.gimbal_backend != "native-full"
    return [
        {
            "name": "scenario",
            "agent": "scenario",
            "deps": [],
            "payload": {"goal": goal, "note": note, "short": short},
        },
        {
            "name": "simulate",
            "agent": "sim_runner",
            "deps": ["scenario"],
            "payload": {"goal": goal},
        },
        {
            "name": "firmware",
            "agent": "firmware",
            "deps": [],
            "payload": {"goal": goal},
        },
        {
            "name": "tune",
            "agent": "tuning",
            "deps": ["scenario", "simulate"],
            "payload": {"goal": goal},
        },
        {
            "name": "setpoint",
            "agent": "setpoint",
            "deps": ["firmware"],
            "risk": "high",
            "payload": {"goal": goal, "pan": pan, "tilt": tilt, "kind": kind},
        },
        {
            "name": "review",
            "agent": "gimbal_reviewer",
            "deps": ["simulate", "firmware", "tune", "setpoint"],
            "dep_policy": "best_effort",
            "payload": {"goal": goal},
        },
    ]


def scenario_agent(task: dict, ctx) -> dict:
    payload = task["payload"]
    note = payload.get("note") or ctx.llm.complete(f"SCENARIO\n{payload.get('goal', '')}")
    document = build_scenario(payload.get("goal", ""), short=bool(payload.get("short", True)), note=note)
    return {
        "status": "succeeded",
        "confidence": 0.95,
        "output": {"scenario": document, "yaml": dump_scenario(document)},
    }


def sim_runner_agent(task: dict, ctx) -> dict:
    scenario = task["payload"]["deps"]["scenario"]["scenario"]
    metrics = None
    native_error = None
    if ctx.settings.gimbal_backend == "native" and ctx.settings.gimbal_root:
        try:
            from pathlib import Path

            work = Path("/tmp") / "swarm-gimbal" / task["id"].replace(":", "_")
            work.mkdir(parents=True, exist_ok=True)
            metrics = run_native(ctx.settings.gimbal_root, scenario, work)
        except (OSError, RuntimeError, TimeoutError) as exc:
            native_error = str(exc)
    if metrics is None:
        metrics = run_short(scenario)
        if native_error:
            metrics["native_error"] = native_error
    confidence = 0.9 if metrics["average_nees"] < 20 else 0.55
    return {"status": "succeeded", "confidence": confidence, "output": metrics}


def firmware_agent(task: dict, ctx) -> dict:
    campaign = run_campaign()
    host = try_host_tests(ctx.settings.firmware_root)
    renode = try_renode_tests(ctx.settings.firmware_root)
    native_ok = True
    if host is not None:
        native_ok = native_ok and bool(host.get("passed"))
    if renode is not None:
        native_ok = native_ok and bool(renode.get("passed"))
    passed = campaign["passed"] and native_ok
    return {
        "status": "succeeded" if passed else "failed",
        "confidence": 0.92 if passed else 0.4,
        "output": {"campaign": campaign, "host": host, "renode": renode},
        "error": None if passed else "firmware campaign failed",
    }


def tuning_agent(task: dict, ctx) -> dict:
    deps = task["payload"]["deps"]
    scenario = deps["scenario"]["scenario"]
    before = deps["simulate"]
    note = ctx.llm.complete(f"TUNE\n{task['payload'].get('goal', '')}")
    gains = dict(scenario["controller"])
    proposal = propose_gains(before, gains)
    proposal["narrative"] = note
    comparison = evaluate(scenario, before, proposal)
    return {"status": "succeeded", "confidence": 0.88, "output": comparison}


def setpoint_agent(task: dict, ctx) -> dict:
    payload = task["payload"]
    proposal = payload.get("proposal")
    token = payload.get("approval_token")
    if proposal and token:
        try:
            sent = ctx.uart.commit(proposal, token)
        except (ToolDisabled, LimitRejected, ApprovalRequired) as exc:
            return {
                "status": "failed",
                "confidence": 1.0,
                "output": {"proposal": proposal, "sent": False, "error": str(exc)},
                "error": str(exc),
            }
        return {
            "status": "succeeded",
            "confidence": 0.97,
            "output": {"proposal": _public_proposal(proposal), "sent": True, "transport": sent},
        }
    proposal = ctx.uart.propose(payload.get("kind", "pos"), float(payload["pan"]), float(payload["tilt"]))
    return {
        "status": "needs_approval",
        "confidence": 1.0,
        "output": {"proposal": _public_proposal(proposal)},
        "proposal": proposal,
        "error": None,
    }


def gimbal_reviewer(task: dict, ctx) -> dict:
    deps = task["payload"].get("deps", {})
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    sim = deps.get("simulate") or {}
    check("nees", _finite(sim.get("average_nees")) and sim.get("average_nees", 99) < 30, f"NEES={sim.get('average_nees')}")
    check(
        "path-rmse",
        _finite(sim.get("position_rmse_m")) and sim.get("position_rmse_m", 99) < 5,
        f"RMSE={sim.get('position_rmse_m')}",
    )
    firmware = deps.get("firmware") or {}
    campaign = (firmware.get("campaign") or {}) if isinstance(firmware, dict) else {}
    check("firmware-campaign", bool(campaign.get("passed")), f"passed={campaign.get('passed_count')}")
    tune = deps.get("tune") or {}
    check("tuning", "before" in tune and "after" in tune, "before/after present" if "before" in tune else "missing")
    setpoint = deps.get("setpoint") or {}
    transport = setpoint.get("transport") or {}
    mode = (transport.get("mode") or (setpoint.get("proposal") or {}).get("mode"))
    sent = bool(setpoint.get("sent"))
    if sent:
        check("setpoint-sim-only", mode in {"sim", "renode"}, f"mode={mode}")
    else:
        check("setpoint-not-sent", True, setpoint.get("error") or "operator or limit held the command")
    narrative = ctx.llm.complete("REVIEW pan/tilt engineering run. Summarize the checks.")
    passed = all(item["passed"] for item in checks)
    report = _report(checks, sim, tune, firmware, setpoint, narrative)
    return {
        "status": "succeeded" if passed else "failed",
        "confidence": 0.91 if passed else 0.48,
        "output": {"checks": checks, "report": report, "passed": passed},
        "error": None if passed else "review checks failed",
    }


def _public_proposal(proposal: dict) -> dict:
    return {
        "kind": proposal.get("kind"),
        "pan": proposal.get("pan"),
        "tilt": proposal.get("tilt"),
        "lines": proposal.get("lines"),
        "mode": proposal.get("mode"),
        "sendable": proposal.get("sendable"),
        "rejection": proposal.get("rejection"),
    }


def _finite(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in {float("inf"), float("-inf")}


def _report(checks, sim, tune, firmware, setpoint, narrative: str) -> str:
    lines = ["# Pan/tilt engineering report", "", narrative, "", "## Checks", ""]
    for item in checks:
        mark = "pass" if item["passed"] else "fail"
        lines.append(f"- {item['name']}: {mark}. {item['detail']}")
    lines.extend(
        [
            "",
            "## Estimator and axis",
            "",
            f"- Backend: {sim.get('backend', 'n/a')}",
            f"- Average NEES: {sim.get('average_nees', 'n/a')}",
            f"- Position RMSE (m): {sim.get('position_rmse_m', 'n/a')}",
            f"- Pan RMS (rad): {sim.get('pan_rms_rad', 'n/a')}",
            f"- Rise time (s): {sim.get('rise_time_s', 'n/a')}",
            f"- Overshoot (%): {sim.get('overshoot_percent', 'n/a')}",
            "",
            "## Tuning",
            "",
            f"- Rationale: {(tune.get('proposal') or {}).get('rationale', 'n/a')}",
            f"- Delta overshoot (percentage points): {(tune.get('delta') or {}).get('overshoot_percent', 'n/a')}",
            "",
            "## Firmware campaign",
            "",
            f"- Mode: {(firmware.get('campaign') or {}).get('mode', 'n/a')}",
            f"- Passed cases: {(firmware.get('campaign') or {}).get('passed_count', 'n/a')}",
            "",
            "## Setpoint audit",
            "",
            f"- Sent: {bool(setpoint.get('sent'))}",
            f"- Mode: {((setpoint.get('transport') or {}).get('mode') or (setpoint.get('proposal') or {}).get('mode'))}",
            f"- Rejection: {(setpoint.get('proposal') or {}).get('rejection') or setpoint.get('error') or 'none'}",
            "",
            "Commands were limited to a sim or Renode shell. No physical positioner was addressed.",
        ]
    )
    return "\n".join(lines)
