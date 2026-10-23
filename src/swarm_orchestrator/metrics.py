"""Run metrics and a Mermaid rendering of the task DAG."""

from __future__ import annotations


def summarize(results: dict) -> dict:
    rows = []
    for task_id, result in results.items():
        rows.append(
            {
                "task_id": task_id,
                "name": result.get("name"),
                "agent": result.get("agent"),
                "status": result.get("status"),
                "retries": int(result.get("retries") or 0),
                "attempts": int(result.get("attempts") or 0),
                "latency_ms": float(result.get("latency_ms") or 0.0),
                "confidence": result.get("confidence"),
            }
        )
    total = len(rows)
    succeeded = sum(row["status"] == "succeeded" for row in rows)
    latencies = [row["latency_ms"] for row in rows]
    return {
        "tasks_total": total,
        "tasks_succeeded": succeeded,
        "tasks_failed": sum(row["status"] in {"failed", "dlq", "rejected"} for row in rows),
        "success_rate": (succeeded / total) if total else 0.0,
        "retries": sum(row["retries"] for row in rows),
        "latency_ms_avg": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "per_task": rows,
    }


def dag_mermaid(tasks: dict, results: dict, inflight: list[str] | None = None, paused: str | None = None) -> str:
    inflight = set(inflight or [])
    lines = ["flowchart TD"]
    for task_id, task in tasks.items():
        if task_id == paused:
            status = "paused"
        elif task_id in results:
            status = results[task_id].get("status", "done")
        elif task_id in inflight:
            status = "running"
        else:
            status = "pending"
        label = f"{task['name']} ({status})"
        lines.append(f'  {task["name"]}["{label}"]')
    for task in tasks.values():
        for dep in task["deps"]:
            lines.append(f"  {tasks[dep]['name']} --> {task['name']}")
    return "\n".join(lines)
