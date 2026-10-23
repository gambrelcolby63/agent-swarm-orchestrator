"""Task templates. The coordinator owns the DAG; the model only fills notes."""

from __future__ import annotations

from swarm_orchestrator.gimbal.team import gimbal_templates


def generic_templates(goal: str) -> list[dict]:
    return [
        {"name": "research", "agent": "researcher", "deps": [], "payload": {"goal": goal}},
        {"name": "draft", "agent": "coder", "deps": ["research"], "payload": {"goal": goal}},
        {"name": "verify", "agent": "reviewer", "deps": ["draft"], "payload": {"goal": goal}},
    ]


class Planner:
    def __init__(self, llm, settings):
        self.llm = llm
        self.settings = settings

    def plan(self, goal: str, team: str) -> list[dict]:
        if team == "gimbal":
            note = self.llm.complete(f"SCENARIO\n{goal}")
            return gimbal_templates(goal, note, self.settings)
        self.llm.complete(f"PLAN\n{goal}")
        return generic_templates(goal)


def normalize_tasks(run_id: str, templates: list[dict], settings) -> dict:
    names = [item["name"] for item in templates]
    if len(names) != len(set(names)):
        raise ValueError("task names must be unique")
    ids = {name: f"{run_id}:{name}" for name in names}
    tasks = {}
    for item in templates:
        missing = [dep for dep in item.get("deps", []) if dep not in ids]
        if missing:
            raise ValueError(f"unknown dependencies: {missing}")
        task_id = ids[item["name"]]
        tasks[task_id] = {
            "id": task_id,
            "name": item["name"],
            "agent": item["agent"],
            "deps": [ids[dep] for dep in item.get("deps", [])],
            "dep_policy": item.get("dep_policy", "all_success"),
            "payload": dict(item.get("payload") or {}),
            "risk": item.get("risk", "low"),
            "timeout_s": float(item.get("timeout_s", settings.task_timeout_s)),
            "max_attempts": int(item.get("max_attempts", settings.max_attempts)),
            "cost": float(item.get("cost", settings.task_cost)),
            "attempt": 1,
            "generation": 0,
        }
    return tasks
