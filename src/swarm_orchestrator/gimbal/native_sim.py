"""Optional adapter for a pinned gimbal-tracker checkout.

The native tools used here are the axis step response and the estimator
Monte Carlo. The closed-loop lead demo is intentionally not invoked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


def run_native(root: str, scenario: dict, work: Path, timeout_s: float = 60.0) -> dict | None:
    base = Path(root)
    estimator = base / "build" / "estimator_mc"
    step = base / "build" / "step_response"
    config_path = base / "config" / "gimbal_tracker.yaml"
    if not estimator.is_file() or not step.is_file() or not config_path.is_file():
        return None
    config = yaml.safe_load(config_path.read_text())
    config["analysis"]["mc_runs"] = int(scenario["analysis"].get("mc_runs", 4))
    config["analysis"]["mc_duration_s"] = float(scenario["analysis"].get("mc_duration_s", 1.0))
    if "maneuver" in scenario:
        config["maneuver"] = scenario["maneuver"]
    written = work / "gimbal_tracker.yaml"
    written.write_text(yaml.safe_dump(config, sort_keys=False))
    step_csv = work / "step.csv"
    est_csv = work / "estimator.csv"
    est_json = work / "estimator.json"
    _run([str(step), str(written), str(step_csv), "--friction"], timeout_s)
    _run([str(estimator), str(written), str(est_csv), str(est_json)], timeout_s)
    metrics = _parse_estimator(est_json.read_text())
    axis = _parse_step(step_csv.read_text(), float(config["analysis"].get("step_rad", 0.25)))
    metrics.update(axis)
    metrics["backend"] = "native"
    metrics["mc_runs"] = int(scenario["analysis"].get("mc_runs", 4))
    return metrics


def _run(argv: list[str], timeout_s: float) -> None:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout_s)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "native sim failed")


def _parse_estimator(text: str) -> dict:
    import json

    payload = json.loads(text)
    return {
        "average_nees": float(payload["average_nees"]),
        "position_rmse_m": float(payload.get("final_pos_rmse", payload.get("final_pos_rmse_m"))),
        "velocity_rmse_m_s": float(payload.get("final_vel_rmse", payload.get("final_vel_rmse_m_s", 0.0))),
    }


def _parse_step(csv_text: str, step: float) -> dict:
    rows = [line.split(",") for line in csv_text.strip().splitlines()[1:] if line.strip()]
    times = [float(row[0]) for row in rows]
    theta = [float(row[1]) for row in rows]
    peak = max(theta) if theta else 0.0
    t10 = next((t for t, value in zip(times, theta) if value >= 0.1 * step), times[-1] if times else 0.0)
    t90 = next((t for t, value in zip(times, theta) if value >= 0.9 * step), times[-1] if times else 0.0)
    err_sq = sum((step - value) ** 2 for value in theta) or 0.0
    count = max(1, len(theta))
    overshoot = max(0.0, (peak - step) / step * 100.0) if step else 0.0
    return {
        "rise_time_s": max(0.0, t90 - t10),
        "overshoot_percent": overshoot,
        "pan_rms_rad": (err_sq / count) ** 0.5,
        "tilt_rms_rad": (err_sq / count) ** 0.5,
    }


def native_available(root: str) -> bool:
    if not root:
        return False
    base = Path(root)
    return (base / "build" / "estimator_mc").is_file() and shutil.which("cmake") is not None
