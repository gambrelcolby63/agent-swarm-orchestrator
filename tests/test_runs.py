from tests.conftest import make_engine


def _names(state):
    return {task_id.split(":")[-1]: result["status"] for task_id, result in state["results"].items()}


def test_generic_team_completes():
    engine = make_engine()
    state = engine.create_run("Summarize the change", team="generic")
    done = engine.pump(state["run_id"])
    assert done["status"] == "completed"
    assert _names(done) == {"research": "succeeded", "draft": "succeeded", "verify": "succeeded"}
    assert done["metrics"]["success_rate"] == 1
    assert done["metrics"]["latency_ms_avg"] >= 0


def test_low_confidence_pauses_until_a_person_decides():
    engine = make_engine()
    state = engine.create_run("Check this low-confidence claim", team="generic")
    paused = engine.pump(state["run_id"])
    assert paused["status"] == "paused"
    assert paused["pause"]["kind"] == "low_confidence"
    engine.decide(state["run_id"], paused["pause"]["id"], "reject", note="insufficient evidence")
    done = engine.pump(state["run_id"])
    assert done["status"] == "completed"
    assert _names(done)["verify"] == "rejected"


def test_budget_and_step_caps_stop_the_run():
    budget = make_engine(budget=1.5)
    state = budget.create_run("too expensive", team="generic")
    done = budget.pump(state["run_id"])
    assert done["status"] == "failed"
    assert done["error"] == "budget"

    steps = make_engine(max_steps=1)
    state = steps.create_run("too many steps", team="generic")
    done = steps.pump(state["run_id"])
    assert done["status"] == "failed"
    assert done["error"] == "max_steps"


def test_cyclic_plan_is_a_deadlock_not_a_spin():
    engine = make_engine()
    plan = [
        {"name": "a", "agent": "probe", "deps": ["b"], "payload": {}},
        {"name": "b", "agent": "probe", "deps": ["a"], "payload": {}},
    ]
    state = engine.create_run("cycle", plan=plan)
    done = engine.pump(state["run_id"])
    assert done["status"] == "failed"
    assert done["error"] == "deadlock"


def test_resume_from_checkpoint_after_a_pause():
    first = make_engine()
    state = first.create_run("Check this low-confidence claim", team="generic", run_id="resume-me")
    paused = first.pump("resume-me")
    assert paused["status"] == "paused"
    second = type(first)(first.ctx, graph=first.graph, runner=first.runner)
    loaded = second.bus.load_checkpoint("resume-me")
    assert loaded["status"] == "paused"
    second.decide("resume-me", paused["pause"]["id"], "approve")
    done = second.pump("resume-me")
    assert done["status"] == "completed"
    assert _names(done)["verify"] == "succeeded"


def test_gimbal_team_pauses_before_any_shell_bytes_and_then_reports():
    engine = make_engine()
    state = engine.create_run("Short pan/tilt check with a weave", team="gimbal", run_id="gimbal-demo")
    paused = engine.pump("gimbal-demo")
    assert paused["status"] == "paused"
    assert paused["pause"]["kind"] == "tool_commit"
    assert engine.ctx.uart.transport.sent == []
    engine.decide("gimbal-demo", paused["pause"]["id"], "approve")
    done = engine.pump("gimbal-demo")
    assert done["status"] == "completed"
    assert _names(done)["setpoint"] == "succeeded"
    assert any(line.startswith("pos ") for line in engine.ctx.uart.transport.sent)
    assert "NEES" in done["report"]
    assert "sim" in done["report"].lower() or "Sim" in done["report"]
    sim = done["results"]["gimbal-demo:simulate"]["output"]
    assert sim["backend"] == "short"
    assert sim["average_nees"] > 0


def test_rejected_and_illegal_setpoints_are_not_sent():
    rejected = make_engine()
    state = rejected.create_run("pan/tilt bench", team="gimbal")
    paused = rejected.pump(state["run_id"])
    rejected.decide(state["run_id"], paused["pause"]["id"], "reject", note="hold")
    done = rejected.pump(state["run_id"])
    assert rejected.ctx.uart.transport.sent == []
    assert _names(done)["setpoint"] == "rejected"
    assert done["status"] == "completed"

    illegal = make_engine()
    state = illegal.create_run("move the positioner pan=200 tilt=0", team="gimbal")
    paused = illegal.pump(state["run_id"])
    assert "limit" in paused["pause"]["reason"].lower() or "not send" in paused["pause"]["reason"].lower()
    illegal.decide(state["run_id"], paused["pause"]["id"], "approve")
    done = illegal.pump(state["run_id"])
    assert illegal.ctx.uart.transport.sent == []
    assert _names(done)["setpoint"] == "failed"
