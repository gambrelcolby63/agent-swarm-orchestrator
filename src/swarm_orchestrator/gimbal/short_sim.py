"""Short pan/tilt plant and constant-velocity filter.

This is the CI and demo backend. It speaks the same metric names as the
pinned C++ simulator (NEES, position RMSE, step rise and overshoot) and
finishes in milliseconds. It does not compute a lead or an intercept.
"""

from __future__ import annotations

import random
from typing import Iterable


def _matvec(m: list[list[float]], v: list[float]) -> list[float]:
    return [sum(a * b for a, b in zip(row, v)) for row in m]


def _add(a: list[float], b: list[float]) -> list[float]:
    return [x + y for x, y in zip(a, b)]


def _sub(a: list[float], b: list[float]) -> list[float]:
    return [x - y for x, y in zip(a, b)]


def _scale(v: list[float], k: float) -> list[float]:
    return [k * x for x in v]


def _outer(a: list[float], b: list[float]) -> list[list[float]]:
    return [[x * y for y in b] for x in a]


def _mm(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    cols = list(zip(*b))
    return [[sum(x * y for x, y in zip(row, col)) for col in cols] for row in a]


def _transpose(m: list[list[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*m)]


def _inv2(m: list[list[float]]) -> list[list[float]]:
    det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
    if abs(det) < 1e-12:
        raise ZeroDivisionError("covariance is singular")
    return [
        [m[1][1] / det, -m[0][1] / det],
        [-m[1][0] / det, m[0][0] / det],
    ]


def _quad(v: list[float], m: list[list[float]]) -> float:
    return sum(v[i] * sum(m[i][j] * v[j] for j in range(len(v))) for i in range(len(v)))


def run_cv_filter(seed: int, steps: int = 50, dt: float = 0.1, sigma_a: float = 0.4, sigma_m: float = 0.5) -> dict:
    """One consistent constant-velocity filter. Mean NEES should sit near 2."""

    rng = random.Random(seed)
    f = [[1.0, dt], [0.0, 1.0]]
    g = [dt * dt / 2.0, dt]
    q = sigma_a * sigma_a
    # Discrete white-acceleration covariance, matching the filter's Q.
    q_discrete = [
        [q * dt**4 / 4.0, q * dt**3 / 2.0],
        [q * dt**3 / 2.0, q * dt**2],
    ]
    h = [[1.0, 0.0]]
    r_meas = sigma_m * sigma_m
    truth = [0.0, 1.0]
    estimate = [0.0, 1.0]
    covariance = [[1.0, 0.0], [0.0, 1.0]]
    nees_sum = 0.0
    pos_sq = 0.0
    vel_sq = 0.0
    for _ in range(steps):
        accel = rng.gauss(0.0, sigma_a)
        truth = _add(_matvec(f, truth), _scale(g, accel))
        estimate = _matvec(f, estimate)
        covariance = _mm(_mm(f, covariance), _transpose(f))
        covariance = [[covariance[i][j] + q_discrete[i][j] for j in range(2)] for i in range(2)]
        measurement = truth[0] + rng.gauss(0.0, sigma_m)
        innovation = measurement - _matvec(h, estimate)[0]
        # S is 1x1.
        pht = [covariance[0][0], covariance[1][0]]
        s = covariance[0][0] + r_meas
        gain = [pht[0] / s, pht[1] / s]
        estimate = _add(estimate, _scale(gain, innovation))
        eye_kh = [
            [1.0 - gain[0], 0.0],
            [0.0 - gain[1], 1.0],
        ]
        # Joseph form.
        updated = _mm(_mm(eye_kh, covariance), _transpose(eye_kh))
        kkt = _outer(gain, _scale(gain, r_meas))
        covariance = [[updated[i][j] + kkt[i][j] for j in range(2)] for i in range(2)]
        error = _sub(estimate, truth)
        nees_sum += _quad(error, _inv2(covariance))
        pos_sq += error[0] ** 2
        vel_sq += error[1] ** 2
    return {
        "average_nees": nees_sum / steps,
        "position_rmse_m": (pos_sq / steps) ** 0.5,
        "velocity_rmse_m_s": (vel_sq / steps) ** 0.5,
    }


def simulate_axis(
    *,
    kp_pos: float,
    kp_rate: float,
    ki_rate: float,
    step: float = 0.25,
    dt: float = 0.002,
    duration: float = 1.2,
    inertia: float = 0.05,
) -> dict:
    """Cascaded P + PI on a DC-motor axis. Angles are radians."""

    viscous = 0.02
    torque_const = 0.1
    resistance = 2.0
    back_emf = 0.1
    rate_limit = 3.0
    voltage_limit = 24.0
    theta = 0.0
    omega = 0.0
    integrator = 0.0
    t = 0.0
    steps = int(duration / dt)
    peak = 0.0
    t10 = None
    t90 = None
    err_sq = 0.0
    for _ in range(steps):
        error = step - theta
        rate_cmd = max(-rate_limit, min(rate_limit, kp_pos * error))
        rate_error = rate_cmd - omega
        unsaturated = kp_rate * rate_error + integrator
        voltage = max(-voltage_limit, min(voltage_limit, unsaturated))
        integrator += (ki_rate * rate_error + 8.0 * (voltage - unsaturated)) * dt
        current = (voltage - back_emf * omega) / resistance
        accel = (torque_const * current - viscous * omega) / inertia
        omega += accel * dt
        theta += omega * dt
        t += dt
        peak = max(peak, theta)
        if t10 is None and theta >= 0.1 * step:
            t10 = t
        if t90 is None and theta >= 0.9 * step:
            t90 = t
        err_sq += error * error
    overshoot = max(0.0, (peak - step) / step * 100.0) if step else 0.0
    rise = (t90 - t10) if t10 is not None and t90 is not None else duration
    return {
        "rise_time_s": rise,
        "overshoot_percent": overshoot,
        "rms_rad": (err_sq / steps) ** 0.5,
        "final_rad": theta,
    }


def run_short(scenario: dict, gains: dict | None = None, seeds: Iterable[int] | None = None) -> dict:
    gains = dict(gains or scenario["controller"])
    analysis = scenario.get("analysis", {})
    runs = int(analysis.get("mc_runs", 4))
    if seeds is None:
        seeds = range(runs)
    seed_list = list(seeds)
    filters = [run_cv_filter(seed) for seed in seed_list]
    axes = []
    for index, seed in enumerate(seed_list):
        # A small inertia spread stands in for plant variation across the campaign.
        inertia = 0.05 * (1.0 + 0.04 * ((seed % 5) - 2))
        pan = simulate_axis(inertia=inertia, **gains)
        tilt = simulate_axis(inertia=inertia * 0.8, **gains)
        axes.append((pan, tilt))
    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    return {
        "backend": "short",
        "average_nees": mean([item["average_nees"] for item in filters]),
        "position_rmse_m": mean([item["position_rmse_m"] for item in filters]),
        "velocity_rmse_m_s": mean([item["velocity_rmse_m_s"] for item in filters]),
        "pan_rms_rad": mean([pan["rms_rad"] for pan, _tilt in axes]),
        "tilt_rms_rad": mean([tilt["rms_rad"] for _pan, tilt in axes]),
        "rise_time_s": mean([pan["rise_time_s"] for pan, _tilt in axes]),
        "overshoot_percent": mean([pan["overshoot_percent"] for pan, _tilt in axes]),
        "mc_runs": len(seed_list),
        "gains": gains,
        "seeds": seed_list,
    }
