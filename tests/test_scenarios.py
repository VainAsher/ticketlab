"""C1: scenario starter set ×5. Written before the scenarios and the
mock_physics schema extension exist.

Contract for EVERY scenario in scenarios/: it loads, its fault leaves the
server unable to hold 'running' (or observably broken), and applying its
full-grade solution verifies as full. This test is parametrized over the
directory, so scenario authoring failures surface in CI, not in a lab.
"""
from pathlib import Path

import pytest

SCEN_DIR = Path(__file__).parent.parent / "scenarios"
ALL = sorted(p.stem for p in SCEN_DIR.glob("*.yaml"))

# Per-scenario recipe for the FULL-grade fix, expressed as mock mutations.
FULL_FIX = {
    "oom-crash-loop-modded": [
        ("set_startup_command", "java -Xms512M -Xmx1024M -jar server.jar")],
    "bad-jvm-flag-wont-start": [
        ("set_startup_command", "java -Xms128M -Xmx2048M -jar server.jar")],
    "wrong-java-version": [("set_variable", ("JAVA_VERSION", "17"))],
    "suspended-not-broken": [("unsuspend", None)],
    "jar-rename-typo": [("set_variable", ("SERVER_JARFILE", "server.jar"))],
}


def build(scenario_id):
    from ticketlab.schema import load_scenario
    from ticketlab.adapters.mock import MockAdapter, FakeClock
    from ticketlab.verifier import Verifier
    s = load_scenario(SCEN_DIR / f"{scenario_id}.yaml")
    clock = FakeClock()
    a = MockAdapter(clock=clock)
    a.provision(s.environment.server, physics=s.environment.mock_physics)
    a.apply_fault(s.fault.steps)
    return s, a, clock, Verifier(s.verification, a, clock)


def test_starter_set_is_five():
    assert len(ALL) == 5, ALL


@pytest.mark.parametrize("sid", ALL)
def test_scenario_loads_and_is_generic(sid):
    from ticketlab.schema import load_scenario
    s = load_scenario(SCEN_DIR / f"{sid}.yaml", product_mode=True)  # firewall-clean
    assert s.conversation.hidden_facts, f"{sid} has no hidden facts"
    assert s.verification.anti_patterns, f"{sid} has no anti-patterns"


@pytest.mark.parametrize("sid", ALL)
def test_broken_state_cannot_verify(sid):
    s, a, clock, v = build(sid)
    a.set_power_state("running")     # trainee mashing Start doesn't help
    clock.advance(400)
    assert v.check().complete is False


@pytest.mark.parametrize("sid", ALL)
def test_full_fix_verifies_full(sid):
    s, a, clock, v = build(sid)
    for action, arg in FULL_FIX[sid]:
        if action == "set_startup_command":
            a.set_startup_command(arg)
        elif action == "set_variable":
            a.set_variable(*arg)
        elif action == "unsuspend":
            a.unsuspend_server()
    a.set_power_state("running")
    clock.advance(400)               # clears every stable_for in the set
    r = v.check()
    assert r.complete and r.grade == "full", (sid, r.assertion_detail)


def test_physics_variable_rule_crashes_server():
    """wrong-java-version: JAVA_VERSION=8 must make 'running' unholdable."""
    s, a, clock, v = build("wrong-java-version")
    a.set_power_state("running")
    assert a.snapshot().power_state == "offline"
    a.set_variable("JAVA_VERSION", "17")
    a.set_power_state("running")
    assert a.snapshot().power_state == "running"


def test_physics_startup_contains_rule():
    s, a, clock, v = build("bad-jvm-flag-wont-start")
    assert "-XX:UseSuperSpeed" in a.snapshot().startup_command
    a.set_power_state("running")
    assert a.snapshot().power_state == "offline"
