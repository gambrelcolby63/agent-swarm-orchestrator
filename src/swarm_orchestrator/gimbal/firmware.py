"""Lightweight firmware campaign and parsers for host / Renode output.

The campaign is a bench model of the documented pan/tilt safety behavior:
e-stop, heartbeat loss, soft limits, keep-out, stall, and overcurrent.
It never talks to a physical board.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from swarm_orchestrator.gimbal.limits import (
    PAN_MAX_DEG,
    PAN_MIN_DEG,
    TILT_MAX_DEG,
    TILT_MIN_DEG,
    Zone,
    path_blocked,
    within_soft_limits,
)


@dataclass
class Bench:
    state: str = "SAFE"
    homed: bool = False
    fault: str = "NONE"
    bridge: bool = False
    pan: float = 0.0
    tilt: float = 0.0
    estop: bool = False
    heartbeat: bool = True
    zones: list[Zone] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def status(self) -> str:
        return (
            f"OK state={self.state} homed={int(self.homed)} fault={self.fault} "
            f"bridge={int(self.bridge)} pan={self.pan:.2f} tilt={self.tilt:.2f}"
        )

    def command(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            reply = "ERR parse"
        else:
            reply = self._dispatch(parts)
        self.replies.append(reply)
        return reply

    def _dispatch(self, parts: list[str]) -> str:
        verb = parts[0]
        if self.estop and verb not in {"reset", "status", "hb"}:
            return "ERR state"
        if verb == "status":
            return self.status()
        if verb == "hb":
            self.heartbeat = True
            return "OK hb"
        if verb == "home":
            if self.state in {"FAULT", "ESTOP"}:
                return "ERR state"
            self.homed = True
            self.state = "SAFE"
            self.bridge = False
            return self.status()
        if verb == "enable":
            if not self.homed:
                return "ERR not-homed"
            if self.state not in {"SAFE", "ENABLED"}:
                return "ERR state"
            self.state = "ENABLED"
            self.bridge = True
            return self.status()
        if verb == "disable":
            self.state = "SAFE"
            self.bridge = False
            return self.status()
        if verb == "pos":
            return self._move(float(parts[1]), float(parts[2]), rate=False)
        if verb == "rate":
            return self._move(float(parts[1]), float(parts[2]), rate=True)
        if verb == "zone" and len(parts) >= 6 and parts[1] == "add":
            zone = Zone(float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5]))
            if not zone.valid() or zone.contains(self.pan, self.tilt):
                return "ERR keep-out"
            self.zones.append(zone)
            return "OK zone"
        if verb == "estop":
            return self.inject_estop(asserted=True)
        if verb == "reset":
            if self.estop:
                return "ERR state"
            if self.state in {"FAULT", "ESTOP"}:
                self.state = "SAFE"
                self.fault = "NONE"
                self.bridge = False
                self.homed = False
            return self.status()
        return "ERR parse"

    def _move(self, pan: float, tilt: float, rate: bool) -> str:
        if self.state not in {"ENABLED", "MOVING"}:
            return "ERR state"
        if rate:
            if abs(pan) > 120 or abs(tilt) > 80:
                return "ERR limit"
            return self.status()
        if not within_soft_limits(pan, tilt):
            return "ERR limit"
        if path_blocked(self.pan, self.tilt, pan, tilt, self.zones):
            return "ERR keep-out"
        self.pan = pan
        self.tilt = tilt
        self.state = "ENABLED"
        return self.status()

    def inject_estop(self, asserted: bool) -> str:
        self.estop = asserted
        if asserted:
            self.state = "ESTOP"
            self.bridge = False
            self.homed = False
            self.fault = "ESTOP"
        return self.status()

    def lose_heartbeat(self) -> str:
        self.heartbeat = False
        if self.state in {"ENABLED", "MOVING", "HOMING"}:
            self.state = "SAFE"
            self.bridge = False
            self.fault = "NONE"
        return self.status()

    def inject_overcurrent(self) -> str:
        self.state = "FAULT"
        self.bridge = False
        self.fault = "OVERCURRENT"
        return self.status()

    def inject_stall(self) -> str:
        self.state = "FAULT"
        self.bridge = False
        self.fault = "STALL"
        return self.status()


def run_campaign() -> dict:
    cases = [
        _case_enable_before_home,
        _case_soft_limit,
        _case_keepout,
        _case_accepted_move,
        _case_estop_latch,
        _case_heartbeat,
        _case_overcurrent,
        _case_stall,
    ]
    results = []
    for case in cases:
        bench = Bench()
        name, ok, detail = case(bench)
        results.append({"name": name, "passed": ok, "detail": detail})
    passed = all(item["passed"] for item in results)
    return {
        "mode": "lightweight",
        "passed": passed,
        "cases": results,
        "passed_count": sum(item["passed"] for item in results),
        "failed_count": sum(not item["passed"] for item in results),
        "limits": {
            "pan_deg": [PAN_MIN_DEG, PAN_MAX_DEG],
            "tilt_deg": [TILT_MIN_DEG, TILT_MAX_DEG],
        },
    }


def _case_enable_before_home(bench: Bench) -> tuple[str, bool, str]:
    reply = bench.command("enable")
    ok = reply == "ERR not-homed" and bench.state == "SAFE" and not bench.bridge
    return "enable-before-home", ok, reply


def _case_soft_limit(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    reply = bench.command("pos 200 0")
    ok = reply == "ERR limit" and bench.pan == 0.0
    return "soft-limit", ok, reply


def _case_keepout(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    bench.command("zone add 40 60 10 30")
    reply = bench.command("pos 50 20")
    ok = reply == "ERR keep-out" and bench.pan == 0.0
    return "keep-out", ok, reply


def _case_accepted_move(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    reply = bench.command("pos 15 8")
    ok = reply.startswith("OK") and bench.pan == 15 and bench.tilt == 8 and bench.bridge
    return "accepted-move", ok, reply


def _case_estop_latch(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    held = bench.inject_estop(True)
    ignored = bench.command("reset")
    bench.inject_estop(False)
    still = bench.state == "ESTOP"
    cleared = bench.command("reset")
    ok = (
        "ESTOP" in held
        and bench.bridge is False
        and ignored.startswith("ERR")
        and still
        and "state=SAFE" in cleared
        and bench.homed is False
    )
    return "estop-latch", ok, cleared


def _case_heartbeat(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    reply = bench.lose_heartbeat()
    ok = "state=SAFE" in reply and bench.fault == "NONE" and bench.bridge is False
    return "heartbeat-loss", ok, reply


def _case_overcurrent(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    reply = bench.inject_overcurrent()
    ok = bench.state == "FAULT" and bench.bridge is False and "OVERCURRENT" in reply
    return "overcurrent", ok, reply


def _case_stall(bench: Bench) -> tuple[str, bool, str]:
    bench.command("home")
    bench.command("enable")
    reply = bench.inject_stall()
    ok = bench.state == "FAULT" and bench.fault == "STALL" and bench.bridge is False
    return "stall", ok, reply


_PASSED_RE = re.compile(r"\[\s+PASSED\s+\]\s+(\d+)\s+tests?")
_FAILED_RE = re.compile(r"\[\s+FAILED\s+\]\s+(\d+)\s+tests?")
_ROBOT_FAIL_RE = re.compile(r"^\s*FAIL\b", re.MULTILINE)


def parse_gtest_output(text: str) -> dict:
    passed = [int(item) for item in _PASSED_RE.findall(text)]
    failed = [int(item) for item in _FAILED_RE.findall(text)]
    passed_n = passed[-1] if passed else 0
    failed_n = failed[-1] if failed else 0
    return {
        "framework": "gtest",
        "passed": failed_n == 0 and passed_n > 0,
        "passed_count": passed_n,
        "failed_count": failed_n,
    }


def parse_robot_output(text: str) -> dict:
    failed = bool(_ROBOT_FAIL_RE.search(text))
    return {"framework": "renode", "passed": not failed and "PASS" in text, "failed": failed}


def try_host_tests(root: str, timeout_s: float = 120.0) -> dict | None:
    if not root:
        return None
    build = __import__("pathlib").Path(root) / "build" / "host"
    if not build.is_dir():
        return None
    completed = subprocess.run(
        ["ctest", "--test-dir", str(build), "--output-on-failure"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    text = completed.stdout + "\n" + completed.stderr
    parsed = parse_gtest_output(text)
    parsed["returncode"] = completed.returncode
    parsed["passed"] = completed.returncode == 0
    parsed["mode"] = "host"
    return parsed


def try_renode_tests(root: str, timeout_s: float = 180.0) -> dict | None:
    if not root:
        return None
    robot = __import__("pathlib").Path(root) / "tests" / "renode" / "gimbal.robot"
    elf = __import__("pathlib").Path(root) / "build" / "fw" / "gimbal.elf"
    if not robot.is_file() or not elf.is_file() or not __import__("shutil").which("renode-test"):
        return None
    completed = subprocess.run(
        ["renode-test", str(robot)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=root,
    )
    text = completed.stdout + "\n" + completed.stderr
    parsed = parse_robot_output(text)
    parsed["returncode"] = completed.returncode
    parsed["passed"] = completed.returncode == 0 and parsed["passed"]
    parsed["mode"] = "renode"
    return parsed
