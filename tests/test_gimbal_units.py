from swarm_orchestrator.gimbal.firmware import parse_gtest_output, parse_robot_output, run_campaign
from swarm_orchestrator.gimbal.limits import Zone, path_blocked, within_soft_limits
from swarm_orchestrator.gimbal.scenario import build_scenario, validate_scenario
from swarm_orchestrator.gimbal.short_sim import run_short
from swarm_orchestrator.gimbal.tuning import propose_gains
from swarm_orchestrator.gimbal.uart import MemoryShell, ToolDisabled, UartPositionerTool


def test_soft_limits_and_keepout_segment():
    assert within_soft_limits(15, 8)
    assert not within_soft_limits(200, 0)
    assert not within_soft_limits(0, -50)
    zone = Zone(40, 60, 10, 30)
    assert path_blocked(0, 0, 50, 20, [zone]) is not None
    assert path_blocked(0, 0, 10, 5, [zone]) is None


def test_scenario_is_a_motion_profile_without_extra_sections():
    document = build_scenario("include a weave", short=True, note="bench note")
    validate_scenario(document)
    kinds = [segment["kind"] for segment in document["maneuver"]["segments"]]
    assert "weave" in kinds
    assert "lead" not in document
    assert set(document) <= {"maneuver", "analysis", "controller", "estimator"}


def test_short_sim_metrics_are_finite():
    scenario = build_scenario("straight", short=True, note="n")
    metrics = run_short(scenario, seeds=[0, 1, 2])
    assert metrics["backend"] == "short"
    assert 0.2 < metrics["average_nees"] < 15
    assert 0 < metrics["position_rmse_m"] < 5
    assert metrics["rise_time_s"] > 0
    proposal = propose_gains(metrics, scenario["controller"])
    assert proposal["gains"]["kp_pos"] > 0


def test_firmware_campaign_passes_and_parsers_read_tool_output():
    campaign = run_campaign()
    assert campaign["passed"]
    assert campaign["passed_count"] == 8
    parsed = parse_gtest_output("[  PASSED  ] 12 tests.\n")
    assert parsed["passed"] and parsed["passed_count"] == 12
    failed = parse_gtest_output("[  FAILED  ] 1 test, listed below:\n")
    assert not failed["passed"]
    assert parse_robot_output("PASS\n")["passed"]
    assert not parse_robot_output("FAIL something\n")["passed"]


def test_uart_refuses_hardware_and_limits_even_after_a_token():
    shell = MemoryShell(mode="hardware")
    tool = UartPositionerTool("hardware", shell)
    proposal = tool.propose("pos", 15, 8)
    assert proposal["sendable"] is False
    with pytest_raises(ToolDisabled):
        tool.commit(proposal, proposal["token"])
    assert shell.sent == []

    limited = UartPositionerTool("sim", MemoryShell())
    bad = limited.propose("pos", 200, 0)
    from swarm_orchestrator.gimbal.uart import LimitRejected

    with pytest_raises(LimitRejected):
        limited.commit(bad, bad["token"])
    assert limited.transport.sent == []

    blocked = UartPositionerTool("sim", MemoryShell(), zones=[Zone(10, 20, 0, 20)])
    inside = blocked.propose("pos", 15, 8)
    with pytest_raises(LimitRejected):
        blocked.commit(inside, inside["token"])
    assert blocked.transport.sent == []


def test_uart_sends_only_with_the_approval_token():
    shell = MemoryShell()
    tool = UartPositionerTool("sim", shell)
    proposal = tool.propose("pos", 15, 8)
    from swarm_orchestrator.gimbal.uart import ApprovalRequired

    with pytest_raises(ApprovalRequired):
        tool.commit(proposal, None)
    assert shell.sent == []
    sent = tool.commit(proposal, proposal["token"])
    assert any(line.startswith("pos 15.0000 8.0000") for line in sent["lines"])
    assert "hb" in shell.sent
    assert shell.sent[0] == "home"


def pytest_raises(exc):
    import pytest

    return pytest.raises(exc)
