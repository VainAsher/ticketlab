"""Phases 2–3: mock adapter + verifier. Written before the modules exist.

Key behaviours under test:
- stable_for_seconds rejects a crash-looping server (the flicker case)
- highest-grade-wins across simultaneously matching solutions
- anti-patterns are historical (activity log doesn't un-happen)
- injectable clock so 180s of stability takes 0s of wall time
"""
from pathlib import Path

SCEN = Path(__file__).parent.parent / "scenarios" / "oom-crash-loop-modded.yaml"


def make_attempt():
    from ticketlab.schema import load_scenario
    from ticketlab.adapters.mock import MockAdapter, FakeClock
    from ticketlab.verifier import Verifier
    s = load_scenario(SCEN)
    clock = FakeClock()
    adapter = MockAdapter(clock=clock)
    adapter.provision(s.environment.server)
    adapter.apply_fault(s.fault.steps)
    return s, adapter, clock, Verifier(s.verification, adapter, clock)


def test_fault_applied_and_broken_state_visible():
    s, adapter, clock, v = make_attempt()
    snap = adapter.snapshot()
    assert "-Xmx4096M" in snap.startup_command
    assert snap.limits["memory"] == 1024


def test_no_solution_matches_while_broken():
    s, adapter, clock, v = make_attempt()
    result = v.check()
    assert result.matched_solution is None
    assert result.complete is False


def test_crash_loop_flicker_does_not_pass_stability():
    """Server that goes running->offline->running never satisfies stable_for."""
    s, adapter, clock, v = make_attempt()
    adapter.set_startup_command("java -Xms512M -Xmx1024M -jar server.jar")
    adapter.set_power_state("running")
    v.check()
    clock.advance(120)
    adapter.set_power_state("offline")   # crash mid-window
    v.check()
    adapter.set_power_state("running")
    v.check()
    clock.advance(120)                   # only 120s since recovery — not 180
    result = v.check()
    assert result.matched_solution is None


def test_root_cause_fix_matches_full_after_stability_window():
    s, adapter, clock, v = make_attempt()
    adapter.set_startup_command("java -Xms512M -Xmx1024M -jar server.jar")
    adapter.set_power_state("running")
    v.check()
    clock.advance(181)
    result = v.check()
    assert result.matched_solution == "right-size-heap"
    assert result.grade == "full"
    assert result.score == 100
    assert result.complete is True


def test_temp_fix_matches_temp_grade():
    s, adapter, clock, v = make_attempt()
    adapter.set_limits(memory=5120)      # raise limit instead of fixing heap
    adapter.set_power_state("running")
    v.check()
    clock.advance(181)
    result = v.check()
    assert result.matched_solution == "raise-container-limit"
    assert result.grade == "temp"
    assert result.score == 55


def test_highest_grade_wins_when_both_match():
    s, adapter, clock, v = make_attempt()
    adapter.set_limits(memory=5120)                                   # temp path
    adapter.set_startup_command("java -Xmx1024M -jar server.jar")     # AND root cause
    adapter.set_power_state("running")
    v.check()
    clock.advance(181)
    result = v.check()
    assert result.grade == "full"
    assert result.score == 100


def test_anti_pattern_is_historical_and_penalises():
    s, adapter, clock, v = make_attempt()
    adapter.reinstall_server()           # logs server:settings.reinstall
    adapter.set_startup_command("java -Xmx1024M -jar server.jar")
    adapter.set_power_state("running")
    v.check()
    clock.advance(181)
    result = v.check()
    assert result.grade == "full"
    assert "reinstalled" in result.anti_patterns_hit
    assert result.score == 100 - 30      # penalty applied to the matched score


def test_customer_observable_filter_hides_internals():
    from ticketlab.statefilter import customer_observable_events
    s, adapter, clock, v = make_attempt()
    before = adapter.snapshot()
    adapter.set_startup_command("java -Xmx1024M -jar server.jar")  # invisible to customer
    adapter.set_power_state("running")                              # visible
    after = adapter.snapshot()
    events = customer_observable_events(before, after)
    joined = " ".join(events).lower()
    assert "online" in joined or "running" in joined
    assert "xmx" not in joined and "startup" not in joined


def test_reinstall_is_customer_observable_as_data_loss():
    from ticketlab.statefilter import customer_observable_events
    s, adapter, clock, v = make_attempt()
    before = adapter.snapshot()
    adapter.reinstall_server()
    after = adapter.snapshot()
    events = customer_observable_events(before, after)
    assert any("reinstall" in e.lower() or "data" in e.lower() for e in events)
