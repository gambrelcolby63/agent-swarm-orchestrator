from pathlib import Path

from swarm_orchestrator.demo import run_demo
from swarm_orchestrator.gimbal.native_sim import _parse_estimator, _parse_step


def test_demo_writes_a_trace(tmp_path: Path):
    trace = run_demo(tmp_path, team="gimbal", approve=True)
    assert trace["status"] == "completed"
    assert trace["shell"]
    assert (tmp_path / "gimbal_trace.json").is_file()
    assert (tmp_path / "gimbal_report.md").is_file()
    assert "flowchart" in (tmp_path / "gimbal_dag.md").read_text()
    assert trace["metrics"]["success_rate"] == 1


def test_native_metric_parsers():
    estimate = _parse_estimator('{"average_nees": 6.0, "final_pos_rmse": 0.2, "final_vel_rmse": 0.4}')
    assert estimate["position_rmse_m"] == 0.2
    step = _parse_step("t,theta,theta_ref,omega,rate_cmd,voltage\n0,0,0.25,0,0,0\n0.2,0.25,0.25,0,0,0\n", 0.25)
    assert step["rise_time_s"] >= 0
    assert step["pan_rms_rad"] >= 0
