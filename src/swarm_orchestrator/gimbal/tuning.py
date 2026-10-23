"""Gain proposals evaluated on the short Monte Carlo plant."""

from __future__ import annotations

from swarm_orchestrator.gimbal.short_sim import run_short

KP_POS_RANGE = (0.5, 12.0)
KP_RATE_RANGE = (0.5, 20.0)
KI_RATE_RANGE = (0.0, 80.0)


def clamp_gains(gains: dict) -> dict:
    return {
        "kp_pos": _clamp(float(gains["kp_pos"]), *KP_POS_RANGE),
        "kp_rate": _clamp(float(gains["kp_rate"]), *KP_RATE_RANGE),
        "ki_rate": _clamp(float(gains["ki_rate"]), *KI_RATE_RANGE),
    }


def propose_gains(metrics: dict, gains: dict) -> dict:
    updated = dict(gains)
    overshoot = float(metrics.get("overshoot_percent", 0.0))
    rise = float(metrics.get("rise_time_s", 1.0))
    rationale = []
    if overshoot > 8.0:
        updated["kp_pos"] = float(updated["kp_pos"]) * 0.85
        updated["kp_rate"] = float(updated["kp_rate"]) * 0.9
        rationale.append("Overshoot is above 8 percent, so position and rate gains were reduced.")
    elif rise > 0.35 and overshoot < 5.0:
        updated["kp_pos"] = float(updated["kp_pos"]) * 1.1
        rationale.append("Rise time is slow and overshoot is small, so the position gain was raised slightly.")
    else:
        rationale.append("Step metrics are already inside the band, so gains were left unchanged.")
    clamped = clamp_gains(updated)
    if clamped != {key: float(updated[key]) for key in clamped}:
        rationale.append("Gains were clamped to the allowed range.")
    return {"gains": clamped, "rationale": " ".join(rationale)}


def evaluate(scenario: dict, before: dict, proposal: dict) -> dict:
    seeds = list(range(int(scenario.get("analysis", {}).get("mc_runs", 4))))
    after = run_short(scenario, gains=proposal["gains"], seeds=seeds)
    return {
        "proposal": proposal,
        "before": _public_metrics(before),
        "after": _public_metrics(after),
        "delta": {
            "overshoot_percent": after["overshoot_percent"] - float(before.get("overshoot_percent", 0.0)),
            "rise_time_s": after["rise_time_s"] - float(before.get("rise_time_s", 0.0)),
            "pan_rms_rad": after["pan_rms_rad"] - float(before.get("pan_rms_rad", 0.0)),
            "average_nees": after["average_nees"] - float(before.get("average_nees", 0.0)),
        },
    }


def _public_metrics(metrics: dict) -> dict:
    keys = (
        "backend",
        "average_nees",
        "position_rmse_m",
        "velocity_rmse_m_s",
        "pan_rms_rad",
        "tilt_rms_rad",
        "rise_time_s",
        "overshoot_percent",
        "mc_runs",
        "gains",
    )
    return {key: metrics[key] for key in keys if key in metrics}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
