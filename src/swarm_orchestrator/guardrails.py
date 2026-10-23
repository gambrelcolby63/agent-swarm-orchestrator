"""Deterministic coordinator guardrails."""

from __future__ import annotations

import hashlib
import json

TERMINAL = {"succeeded", "failed", "dlq", "rejected", "skipped"}


def fingerprint(state: dict) -> str:
    done = sorted((key, value.get("status")) for key, value in state.get("results", {}).items())
    inflight = sorted(state.get("inflight", []))
    phase = state.get("phase")
    raw = json.dumps({"phase": phase, "done": done, "inflight": inflight}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def note_fingerprint(state: dict, limit: int = 3) -> str | None:
    """Record the progress signature. The same signature ``limit`` times is a loop."""

    token = fingerprint(state)
    history = list(state.get("fingerprints", []))
    history.append(token)
    state["fingerprints"] = history
    if history.count(token) >= limit:
        return "loop_detected"
    return None


def deps_ready(task: dict, results: dict) -> bool:
    policy = task.get("dep_policy", "all_success")
    for dep in task["deps"]:
        status = results.get(dep, {}).get("status")
        if policy == "best_effort":
            if status not in TERMINAL:
                return False
        elif status != "succeeded":
            return False
    return True
