"""HTTP API and the small dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from swarm_orchestrator.engine import Engine
from swarm_orchestrator.metrics import dag_mermaid, summarize

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("swarm")


class CreateRun(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    team: str = "generic"
    wait: bool = False


class Decision(BaseModel):
    decision: str
    note: str = ""
    wait: bool = False


class Service:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            if run_id not in self._locks:
                self._locks[run_id] = threading.Lock()
            return self._locks[run_id]

    def pump(self, run_id: str) -> None:
        with self.lock(run_id):
            try:
                self.engine.pump(run_id)
            except Exception:
                log.exception("pump failed for %s", run_id)

    def start(self, run_id: str) -> None:
        threading.Thread(target=self.pump, args=(run_id,), name=f"pump-{run_id}", daemon=True).start()

    def recover(self) -> None:
        for run_id, status in self.engine.bus.list_runs():
            if status == "running":
                self.start(run_id)


def create_app(engine: Engine | None = None) -> FastAPI:
    engine = engine or Engine.create()
    service = Service(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.recover()
        yield

    app = FastAPI(title="Swarm Orchestrator", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.service = service

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "llm": engine.settings.llm, "uart_mode": engine.settings.uart_mode}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/runs")
    def create_run(body: CreateRun) -> dict:
        if body.team not in {"generic", "gimbal"}:
            raise HTTPException(400, "team must be generic or gimbal")
        state = engine.create_run(body.goal, team=body.team)
        if body.wait:
            with service.lock(state["run_id"]):
                state = engine.pump(state["run_id"])
        else:
            service.start(state["run_id"])
        return public_state(state)

    @app.get("/api/runs")
    def list_runs() -> dict:
        runs = []
        for run_id, status in engine.bus.list_runs():
            state = engine.bus.load_checkpoint(run_id)
            if state is None:
                continue
            runs.append(
                {
                    "run_id": run_id,
                    "status": status,
                    "team": state.get("team"),
                    "goal": state.get("goal"),
                }
            )
        return {"runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return public_state(_load(engine, run_id))

    @app.get("/api/runs/{run_id}/dag")
    def get_dag(run_id: str) -> dict:
        state = _load(engine, run_id)
        paused = (state.get("pause") or {}).get("task_id")
        return {
            "mermaid": dag_mermaid(state.get("tasks") or {}, state.get("results") or {}, state.get("inflight"), paused),
            "tasks": public_state(state)["tasks"],
        }

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, once: bool = False):
        _load(engine, run_id)

        async def stream():
            last = 0
            while True:
                if await request.is_disconnected():
                    break
                batch = engine.bus.events_after(run_id, last)
                for event in batch:
                    last = event["seq"]
                    safe = _public_event(event)
                    yield f"event: {safe['type']}\ndata: {json.dumps(safe)}\n\n"
                state = engine.bus.load_checkpoint(run_id)
                status = state.get("status") if state else "missing"
                if not batch:
                    yield f"event: snapshot\ndata: {json.dumps({'status': status})}\n\n"
                    if status in {"completed", "failed"} or once:
                        break
                await asyncio.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/breakpoints/{breakpoint_id}")
    def decide(run_id: str, breakpoint_id: str, body: Decision) -> dict:
        try:
            with service.lock(run_id):
                state = engine.decide(run_id, breakpoint_id, body.decision, body.note)
                if body.wait:
                    state = engine.pump(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not body.wait:
            service.start(run_id)
        return public_state(engine.bus.load_checkpoint(run_id) or state)

    @app.get("/api/metrics")
    def metrics() -> dict:
        combined = []
        for run_id, _status in engine.bus.list_runs():
            state = engine.bus.load_checkpoint(run_id)
            if not state:
                continue
            current = state.get("metrics") or summarize(state.get("results") or {})
            for row in current.get("per_task") or []:
                item = dict(row)
                item["run_id"] = run_id
                combined.append(item)
        total = len(combined)
        succeeded = sum(row.get("status") == "succeeded" for row in combined)
        latencies = [float(row.get("latency_ms") or 0) for row in combined]
        return {
            "tasks_total": total,
            "tasks_succeeded": succeeded,
            "success_rate": (succeeded / total) if total else 0.0,
            "retries": sum(int(row.get("retries") or 0) for row in combined),
            "latency_ms_avg": (sum(latencies) / len(latencies)) if latencies else 0.0,
            "per_task": combined,
        }

    return app


def _load(engine: Engine, run_id: str) -> dict:
    state = engine.bus.load_checkpoint(run_id)
    if state is None:
        raise HTTPException(404, "run not found")
    return state


def public_state(state: dict) -> dict:
    tasks = []
    results = state.get("results") or {}
    inflight = set(state.get("inflight") or [])
    paused = (state.get("pause") or {}).get("task_id")
    for task_id, task in (state.get("tasks") or {}).items():
        if task_id == paused:
            status = "paused"
        elif task_id in results:
            status = results[task_id].get("status")
        elif task_id in inflight:
            status = "running"
        else:
            status = "pending"
        tasks.append(
            {
                "id": task_id,
                "name": task["name"],
                "agent": task["agent"],
                "deps": [state["tasks"][dep]["name"] for dep in task["deps"] if dep in state["tasks"]],
                "risk": task.get("risk", "low"),
                "status": status,
                "retries": int((results.get(task_id) or {}).get("retries") or 0),
                "latency_ms": (results.get(task_id) or {}).get("latency_ms"),
                "confidence": (results.get(task_id) or {}).get("confidence"),
            }
        )
    pause = state.get("pause")
    public_pause = None
    if pause:
        proposal = dict(pause.get("proposal") or {})
        proposal.pop("token", None)
        public_pause = {
            "id": pause["id"],
            "task_id": pause["task_id"],
            "kind": pause["kind"],
            "reason": pause["reason"],
            "proposal": proposal or None,
        }
    return {
        "run_id": state["run_id"],
        "goal": state.get("goal"),
        "team": state.get("team"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "error": state.get("error"),
        "step": state.get("step"),
        "spent": state.get("spent"),
        "budget": state.get("budget"),
        "tasks": tasks,
        "pause": public_pause,
        "metrics": state.get("metrics") or {},
        "report": state.get("report"),
    }


def _public_event(event: dict) -> dict:
    data = dict(event.get("data") or {})
    proposal = data.get("proposal")
    if isinstance(proposal, dict):
        proposal = dict(proposal)
        proposal.pop("token", None)
        data["proposal"] = proposal
    return {
        "seq": event.get("seq"),
        "ts": event.get("ts"),
        "run_id": event.get("run_id"),
        "type": event.get("type"),
        "message": event.get("message"),
        "data": data,
    }


def main() -> None:
    import uvicorn

    from swarm_orchestrator.settings import Settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    uvicorn.run(
        "swarm_orchestrator.api:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
    )


if __name__ == "__main__":
    main()
