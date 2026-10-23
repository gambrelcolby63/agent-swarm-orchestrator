"""Shared objects passed into the graph and the workers."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from swarm_orchestrator.bus import RedisBus
from swarm_orchestrator.gimbal.uart import MemoryShell, TcpShell, UartPositionerTool
from swarm_orchestrator.llm import LLM, build_llm
from swarm_orchestrator.planning import Planner
from swarm_orchestrator.settings import Settings


@dataclass
class AgentContext:
    llm: LLM
    settings: Settings
    bus: RedisBus
    uart: UartPositionerTool
    cancel: threading.Event


@dataclass
class AppContext:
    bus: RedisBus
    planner: Planner
    settings: Settings
    registry: dict
    uart: UartPositionerTool
    llm: LLM
    rng: random.Random


def build_uart(settings: Settings) -> UartPositionerTool:
    if settings.uart_mode == "renode" and settings.renode_uart:
        transport = TcpShell(settings.renode_uart)
    else:
        transport = MemoryShell(mode=settings.uart_mode)
    return UartPositionerTool(settings.uart_mode, transport)


def build_context(settings: Settings, client=None, uart: UartPositionerTool | None = None) -> AppContext:
    from swarm_orchestrator.agents import build_registry

    bus = RedisBus(client) if client is not None else RedisBus.connect(settings.redis_url)
    llm = build_llm(settings)
    tool = uart if uart is not None else build_uart(settings)
    return AppContext(
        bus=bus,
        planner=Planner(llm, settings),
        settings=settings,
        registry=build_registry(),
        uart=tool,
        llm=llm,
        rng=random.Random(0),
    )
