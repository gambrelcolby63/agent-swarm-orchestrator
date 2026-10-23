"""Travel limits and keep-out geometry for the pan/tilt positioner.

Angles are degrees at the axis output. These match the firmware defaults:
pan soft travel is -170 to +170 degrees, tilt is -45 to +90, and rate caps
are 120 deg/s (pan) and 80 deg/s (tilt). A human approval cannot widen them.
"""

from __future__ import annotations

from dataclasses import dataclass


PAN_MIN_DEG = -170.0
PAN_MAX_DEG = 170.0
TILT_MIN_DEG = -45.0
TILT_MAX_DEG = 90.0
PAN_RATE_MAX_DPS = 120.0
TILT_RATE_MAX_DPS = 80.0


@dataclass(frozen=True)
class Zone:
    pan_min: float
    pan_max: float
    tilt_min: float
    tilt_max: float

    def valid(self) -> bool:
        return self.pan_min < self.pan_max and self.tilt_min < self.tilt_max

    def contains(self, pan: float, tilt: float) -> bool:
        return self.pan_min <= pan <= self.pan_max and self.tilt_min <= tilt <= self.tilt_max


def within_soft_limits(pan: float, tilt: float) -> bool:
    return PAN_MIN_DEG <= pan <= PAN_MAX_DEG and TILT_MIN_DEG <= tilt <= TILT_MAX_DEG


def within_rate_limits(pan_dps: float, tilt_dps: float) -> bool:
    return abs(pan_dps) <= PAN_RATE_MAX_DPS and abs(tilt_dps) <= TILT_RATE_MAX_DPS


def segment_hits_zone(pan0: float, tilt0: float, pan1: float, tilt1: float, zone: Zone) -> bool:
    """True when the straight segment in pan/tilt space meets an axis-aligned zone."""

    return _segment_hits_rect(
        pan0,
        tilt0,
        pan1,
        tilt1,
        zone.pan_min,
        zone.pan_max,
        zone.tilt_min,
        zone.tilt_max,
    )


def path_blocked(pan0: float, tilt0: float, pan1: float, tilt1: float, zones: list[Zone]) -> Zone | None:
    for zone in zones:
        if segment_hits_zone(pan0, tilt0, pan1, tilt1, zone):
            return zone
    return None


def _segment_hits_rect(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
) -> bool:
    dx = x1 - x0
    dy = y1 - y0
    u1 = 0.0
    u2 = 1.0
    for origin, delta, lower, upper in ((x0, dx, xmin, xmax), (y0, dy, ymin, ymax)):
        if abs(delta) < 1e-12:
            if origin < lower or origin > upper:
                return False
            continue
        a = (lower - origin) / delta
        b = (upper - origin) / delta
        if a > b:
            a, b = b, a
        u1 = max(u1, a)
        u2 = min(u2, b)
        if u1 > u2:
            return False
    return True
