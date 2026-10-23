"""UART setpoint adapter.

Position and rate lines are sent only to a sim or Renode shell, only after
a human approval token, and only when the pose stays inside soft limits and
keep-out zones. Real hardware is refused. Approval does not override a limit.
"""

from __future__ import annotations

import secrets
import socket
from dataclasses import dataclass, field

from swarm_orchestrator.gimbal.limits import (
    Zone,
    path_blocked,
    within_rate_limits,
    within_soft_limits,
)

SIM_MODES = {"sim", "renode"}


class ToolDisabled(RuntimeError):
    pass


class ApprovalRequired(RuntimeError):
    pass


class LimitRejected(RuntimeError):
    pass


@dataclass
class MemoryShell:
    """In-process stand-in for the firmware command shell."""

    mode: str = "sim"
    pan: float = 0.0
    tilt: float = 0.0
    homed: bool = False
    state: str = "SAFE"
    sent: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def send_lines(self, lines: list[str]) -> list[str]:
        replies = []
        for line in lines:
            self.sent.append(line)
            replies.append(self._one(line))
        self.replies.extend(replies)
        return replies

    def _one(self, line: str) -> str:
        parts = line.split()
        verb = parts[0]
        if verb == "home":
            self.homed = True
            self.state = "SAFE"
            return "OK state=SAFE homed=1"
        if verb == "enable":
            if not self.homed:
                return "ERR not-homed"
            self.state = "ENABLED"
            return "OK state=ENABLED homed=1"
        if verb == "hb":
            return "OK hb"
        if verb == "status":
            return f"OK state={self.state} homed={int(self.homed)} pan={self.pan:.2f} tilt={self.tilt:.2f}"
        if verb in {"pos", "rate"}:
            if self.state not in {"ENABLED", "MOVING"}:
                return "ERR state"
            pan = float(parts[1])
            tilt = float(parts[2])
            if verb == "pos":
                self.pan = pan
                self.tilt = tilt
                self.state = "ENABLED"
            return f"OK state={self.state} pan={self.pan:.2f} tilt={self.tilt:.2f}"
        return "ERR parse"


class TcpShell:
    """Line socket used when a Renode UART is actually listening."""

    def __init__(self, address: str):
        host, port = address.rsplit(":", 1)
        self.host = host
        self.port = int(port)

    def send_lines(self, lines: list[str]) -> list[str]:
        replies = []
        with socket.create_connection((self.host, self.port), timeout=2.0) as sock:
            file = sock.makefile("rwb", buffering=0)
            for line in lines:
                file.write((line + "\n").encode())
                raw = file.readline()
                replies.append(raw.decode().strip())
        return replies


class UartPositionerTool:
    def __init__(self, mode: str, transport=None, zones: list[Zone] | None = None):
        self.mode = mode.strip().lower()
        self.transport = transport if transport is not None else MemoryShell(mode=self.mode)
        self.zones = list(zones or [Zone(140.0, 160.0, 40.0, 60.0)])
        self.disabled_reason = None
        if self.mode not in SIM_MODES:
            self.disabled_reason = (
                f"Position commands are disabled for mode {self.mode!r}. "
                "Only sim and Renode shells are allowed."
            )

    def propose(self, kind: str, pan: float, tilt: float) -> dict:
        if kind not in {"pos", "rate"}:
            raise ValueError("only pos and rate setpoints are supported")
        rejection = self._rejection(kind, pan, tilt)
        lines = self._lines(kind, pan, tilt)
        return {
            "kind": kind,
            "pan": pan,
            "tilt": tilt,
            "lines": lines,
            "mode": self.mode,
            "token": secrets.token_hex(8),
            "sendable": rejection is None and self.disabled_reason is None,
            "rejection": rejection or self.disabled_reason,
            "zones": [
                {
                    "pan_min": zone.pan_min,
                    "pan_max": zone.pan_max,
                    "tilt_min": zone.tilt_min,
                    "tilt_max": zone.tilt_max,
                }
                for zone in self.zones
            ],
        }

    def commit(self, proposal: dict, approval_token: str | None) -> dict:
        if not approval_token or approval_token != proposal.get("token"):
            raise ApprovalRequired("A matching human approval token is required before any byte is sent.")
        if self.disabled_reason or self.mode not in SIM_MODES:
            raise ToolDisabled(self.disabled_reason or "disabled")
        rejection = self._rejection(proposal["kind"], float(proposal["pan"]), float(proposal["tilt"]))
        if rejection:
            raise LimitRejected(rejection)
        if not proposal.get("sendable", False):
            raise LimitRejected(proposal.get("rejection") or "command is not sendable")
        replies = self.transport.send_lines(list(proposal["lines"]))
        return {"mode": self.mode, "replies": replies, "lines": list(proposal["lines"])}

    def _rejection(self, kind: str, pan: float, tilt: float) -> str | None:
        if self.disabled_reason:
            return self.disabled_reason
        if kind == "pos":
            if not within_soft_limits(pan, tilt):
                return "ERR limit: pose is outside soft travel"
            zone = path_blocked(0.0, 0.0, pan, tilt, self.zones)
            if zone is not None:
                return "ERR keep-out: segment enters a keep-out zone"
        elif not within_rate_limits(pan, tilt):
            return "ERR limit: rate is outside the axis cap"
        return None

    @staticmethod
    def _lines(kind: str, pan: float, tilt: float) -> list[str]:
        motion = f"{kind} {pan:.4f} {tilt:.4f}"
        return ["home", "enable", motion, "hb", "status"]
