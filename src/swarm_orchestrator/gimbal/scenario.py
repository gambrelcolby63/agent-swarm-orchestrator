"""Maneuver-profile YAML for the pan/tilt simulator.

The document is a reference motion for the gimbal axes and the estimator.
It is not a pointing solution against anything physical.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

ALLOWED_KINDS = {"straight", "coordinated_turn", "speed_change", "weave", "circle"}
ALLOWED_TOP = {"maneuver", "analysis", "controller", "estimator"}

_PAN_RE = re.compile(r"pan\s*=\s*(-?\d+(?:\.\d+)?)")
_TILT_RE = re.compile(r"tilt\s*=\s*(-?\d+(?:\.\d+)?)")


def default_gains() -> dict[str, float]:
    return {"kp_pos": 4.0, "kp_rate": 6.0, "ki_rate": 40.0}


def parse_setpoint(goal: str) -> tuple[float, float, str]:
    """Pull an optional bench pose out of the goal text. Defaults stay inside soft limits."""

    pan = 15.0
    tilt = 8.0
    kind = "pos"
    pan_match = _PAN_RE.search(goal)
    tilt_match = _TILT_RE.search(goal)
    if pan_match:
        pan = float(pan_match.group(1))
    if tilt_match:
        tilt = float(tilt_match.group(1))
    if "rate setpoint" in goal.lower() or "kind=rate" in goal.lower():
        kind = "rate"
    return pan, tilt, kind


def build_scenario(goal: str, *, short: bool, note: str) -> dict[str, Any]:
    text = goal.lower()
    if short:
        segments = [
            {"kind": "straight", "duration_s": 0.4},
            {"kind": "coordinated_turn", "duration_s": 0.5, "turn_rate_rad_s": 0.3},
            {"kind": "speed_change", "duration_s": 0.3, "speed_m_s": 16.0},
        ]
        if "weave" in text:
            segments.append(
                {
                    "kind": "weave",
                    "duration_s": 0.4,
                    "weave_amplitude_rad": 0.2,
                    "weave_omega_rad_s": 1.0,
                }
            )
        analysis = {"mc_runs": 4, "mc_duration_s": 1.0, "mc_dt_s": 0.05, "step_rad": 0.25}
    else:
        segments = [
            {"kind": "straight", "duration_s": 4.0},
            {"kind": "coordinated_turn", "duration_s": 5.0, "turn_rate_rad_s": 0.35},
            {"kind": "straight", "duration_s": 3.0},
            {"kind": "speed_change", "duration_s": 2.0, "speed_m_s": 18.0},
            {
                "kind": "weave",
                "duration_s": 4.0,
                "weave_amplitude_rad": 0.35,
                "weave_omega_rad_s": 1.1,
            },
        ]
        analysis = {"mc_runs": 20, "mc_duration_s": 5.0, "mc_dt_s": 0.05, "step_rad": 0.25}
    document = {
        "maneuver": {
            "trajectory": "sequence",
            "position_m": [30.0, -20.0, 4.0],
            "velocity_m_s": [0.0, 12.0, 0.0],
            "acceleration_m_s2": [0.0, 0.0, 0.0],
            "segments": segments,
            "note": note[:500],
        },
        "analysis": analysis,
        "controller": default_gains(),
        "estimator": {"model": "cv", "accel_std_m_s2": 0.4, "cartesian_std_m": 0.5},
    }
    validate_scenario(document)
    return document


def validate_scenario(document: dict) -> None:
    unknown = set(document) - ALLOWED_TOP
    if unknown:
        raise ValueError(f"scenario has unsupported sections: {sorted(unknown)}")
    maneuver = document["maneuver"]
    if maneuver.get("trajectory") != "sequence":
        raise ValueError("only sequence maneuvers are generated")
    segments = maneuver.get("segments") or []
    if not segments:
        raise ValueError("maneuver needs at least one segment")
    total = 0.0
    for segment in segments:
        kind = segment.get("kind")
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported maneuver kind: {kind}")
        duration = float(segment["duration_s"])
        if not 0 < duration <= 60:
            raise ValueError("segment duration must be in (0, 60] seconds")
        total += duration
        if "turn_rate_rad_s" in segment and abs(float(segment["turn_rate_rad_s"])) > 2:
            raise ValueError("turn rate is outside the sim clamp")
    if total > 120:
        raise ValueError("maneuver is longer than 120 seconds")
    gains = document["controller"]
    for key in ("kp_pos", "kp_rate", "ki_rate"):
        if float(gains[key]) < 0:
            raise ValueError("gains must be non-negative")


def dump_scenario(document: dict) -> str:
    return yaml.safe_dump(document, sort_keys=False)
