from fastapi.testclient import TestClient

from swarm_orchestrator.api import create_app
from tests.conftest import make_engine


def test_api_generic_run_sse_and_metrics():
    app = create_app(make_engine())
    with TestClient(app) as client:
        created = client.post("/api/runs", json={"goal": "Write a note", "team": "generic", "wait": True})
        assert created.status_code == 200
        body = created.json()
        assert body["status"] == "completed"
        assert {task["name"] for task in body["tasks"]} == {"research", "draft", "verify"}
        dag = client.get(f"/api/runs/{body['run_id']}/dag")
        assert "research --> draft" in dag.json()["mermaid"]
        events = client.get(f"/api/runs/{body['run_id']}/events?once=1")
        assert events.status_code == 200
        assert "text/event-stream" in events.headers["content-type"]
        assert "run_completed" in events.text
        metrics = client.get("/api/metrics").json()
        assert metrics["tasks_succeeded"] == 3
        assert metrics["success_rate"] == 1
        page = client.get("/")
        assert page.status_code == 200
        assert "Simulation and bench tests only" in page.text
        assert client.get("/health").json()["ok"] is True


def test_api_gimbal_breakpoint_round_trip():
    app = create_app(make_engine())
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"goal": "Pan/tilt short check", "team": "gimbal", "wait": True},
        )
        body = created.json()
        assert body["status"] == "paused"
        assert body["pause"]["kind"] == "tool_commit"
        assert "token" not in (body["pause"].get("proposal") or {})
        decided = client.post(
            f"/api/runs/{body['run_id']}/breakpoints/{body['pause']['id']}",
            json={"decision": "approve", "note": "bench", "wait": True},
        )
        finished = decided.json()
        assert finished["status"] == "completed"
        assert finished["report"]
        assert "NEES" in finished["report"]
        names = {task["name"]: task["status"] for task in finished["tasks"]}
        assert names["setpoint"] == "succeeded"
        assert names["review"] == "succeeded"
