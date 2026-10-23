"""Runtime settings. Defaults keep tests and the demo free of API keys."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass
class Settings:
    llm: str = "mock"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    redis_url: str = "redis://localhost:6379/0"
    max_steps: int = 24
    max_loops: int = 48
    budget: float = 40.0
    plan_cost: float = 1.0
    task_cost: float = 1.0
    confidence_threshold: float = 0.75
    task_timeout_s: float = 10.0
    max_attempts: int = 3
    visibility_ms: int = 400
    backoff_base_s: float = 0.02
    backoff_cap_s: float = 2.0
    gimbal_backend: str = "short"
    gimbal_root: str = ""
    firmware_root: str = ""
    uart_mode: str = "sim"
    renode_uart: str = ""
    short_mc_runs: int = 4
    embedded_workers: bool = True
    allow_chaos: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        embedded = os.environ.get("SWARM_EMBEDDED_WORKERS", "1").strip().lower()
        chaos = os.environ.get("SWARM_ALLOW_CHAOS", "0").strip().lower()
        return cls(
            llm=os.environ.get("SWARM_LLM", "mock").strip().lower(),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            max_steps=_env_int("SWARM_MAX_STEPS", 24),
            max_loops=_env_int("SWARM_MAX_LOOPS", 48),
            budget=_env_float("SWARM_BUDGET", 40.0),
            plan_cost=_env_float("SWARM_PLAN_COST", 1.0),
            task_cost=_env_float("SWARM_TASK_COST", 1.0),
            confidence_threshold=_env_float("SWARM_CONFIDENCE_THRESHOLD", 0.75),
            task_timeout_s=_env_float("SWARM_TASK_TIMEOUT_S", 10.0),
            max_attempts=_env_int("SWARM_MAX_ATTEMPTS", 3),
            visibility_ms=_env_int("SWARM_VISIBILITY_MS", 400),
            backoff_base_s=_env_float("SWARM_BACKOFF_BASE_S", 0.02),
            backoff_cap_s=_env_float("SWARM_BACKOFF_CAP_S", 2.0),
            gimbal_backend=os.environ.get("SWARM_GIMBAL_BACKEND", "short").strip().lower(),
            gimbal_root=os.environ.get("GIMBAL_TRACKER_ROOT", ""),
            firmware_root=os.environ.get("STM32_FW_ROOT", ""),
            uart_mode=os.environ.get("SWARM_UART_MODE", "sim").strip().lower(),
            renode_uart=os.environ.get("SWARM_RENODE_UART", ""),
            short_mc_runs=_env_int("SWARM_SHORT_MC_RUNS", 4),
            embedded_workers=embedded in {"1", "true", "yes"},
            allow_chaos=chaos in {"1", "true", "yes"},
            api_host=os.environ.get("SWARM_API_HOST", "0.0.0.0"),
            api_port=_env_int("SWARM_API_PORT", 8000),
        )
