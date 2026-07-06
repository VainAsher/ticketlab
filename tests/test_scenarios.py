"""C1: scenario starter set. Written before the scenarios and the
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

# Per-scenario recipe for the FULL-grade fix, expressed as adapter mutations.
# Billing actions run against the billing adapter; everything else against
# the game adapter — see build()'s dispatch.
FULL_FIX = {
    "oom-crash-loop-modded": [
        ("set_startup_command", "java -Xms512M -Xmx1024M -jar server.jar")],
    "bad-jvm-flag-wont-start": [
        ("set_startup_command", "java -Xms128M -Xmx2048M -jar server.jar")],
    "wrong-java-version": [("set_variable", ("JAVA_VERSION", "17"))],
    "jar-rename-typo": [("set_variable", ("SERVER_JARFILE", "server.jar"))],
    "suspended-not-broken": [
        ("update_card", ("4242", 6, 2030)),
        ("retry_payment", "inv-lena-042")],
    "billing-overdue-grace-period": [
        ("retry_payment", "inv-nova-118")],
    "billing-payment-fixed-needs-retry": [
        ("retry_payment", "inv-ruby-207")],
}


def build(scenario_id):
    from ticketlab.schema import load_scenario
    from ticketlab.adapters.mock import MockAdapter, FakeClock
    from ticketlab.adapters.billing import BillingAdapter
    from ticketlab.verifier import Verifier
    s = load_scenario(SCEN_DIR / f"{scenario_id}.yaml")
    clock = FakeClock()
    a = MockAdapter(clock=clock)
    billing = BillingAdapter()
    a.provision(s.environment.server, physics=s.environment.mock_physics)
    a.apply_fault(s.fault.steps)
    billing.apply_fault(s.fault.steps)
    a.billing_gate = lambda: billing.account_status == "active"
    return s, a, billing, clock, Verifier(s.verification, a, clock, billing=billing)


def test_starter_set_is_seven():
    assert len(ALL) == 7, ALL


@pytest.mark.parametrize("sid", ALL)
def test_scenario_loads_and_is_generic(sid):
    from ticketlab.schema import load_scenario
    s = load_scenario(SCEN_DIR / f"{sid}.yaml", product_mode=True)  # firewall-clean
    assert s.conversation.hidden_facts, f"{sid} has no hidden facts"
    assert s.verification.anti_patterns, f"{sid} has no anti-patterns"


@pytest.mark.parametrize("sid", ALL)
def test_broken_state_cannot_verify(sid):
    s, a, billing, clock, v = build(sid)
    a.set_power_state("running")     # trainee mashing Start doesn't help
    clock.advance(400)
    assert v.check().complete is False


@pytest.mark.parametrize("sid", ALL)
def test_full_fix_verifies_full(sid):
    s, a, billing, clock, v = build(sid)
    for action, arg in FULL_FIX[sid]:
        if action == "set_startup_command":
            a.set_startup_command(arg)
        elif action == "set_variable":
            a.set_variable(*arg)
        elif action == "update_card":
            billing.update_payment_method(*arg)
        elif action == "retry_payment":
            billing.retry_payment(arg)
    a.set_power_state("running")
    clock.advance(400)               # clears every stable_for in the set
    r = v.check()
    assert r.complete and r.grade == "full", (sid, r.assertion_detail)


def test_physics_variable_rule_crashes_server():
    """wrong-java-version: JAVA_VERSION=8 must make 'running' unholdable."""
    s, a, billing, clock, v = build("wrong-java-version")
    a.set_power_state("running")
    assert a.snapshot().power_state == "offline"
    a.set_variable("JAVA_VERSION", "17")
    a.set_power_state("running")
    assert a.snapshot().power_state == "running"


def test_physics_startup_contains_rule():
    s, a, billing, clock, v = build("bad-jvm-flag-wont-start")
    assert "-XX:UseSuperSpeed" in a.snapshot().startup_command
    a.set_power_state("running")
    assert a.snapshot().power_state == "offline"


def test_billing_suspension_blocks_start_until_account_active():
    """suspended-not-broken: Start must stay denied purely because billing
    says suspended, independent of anything in the game panel."""
    s, a, billing, clock, v = build("suspended-not-broken")
    a.set_power_state("running")
    assert a.snapshot().power_state == "suspended"
    billing.update_payment_method("4242", 6, 2030)
    a.set_power_state("running")
    assert a.snapshot().power_state == "suspended", "card alone isn't enough — invoice still unpaid"
    billing.retry_payment("inv-lena-042")
    a.set_power_state("running")
    assert a.snapshot().power_state == "running"


def test_retry_payment_fails_silently_on_invalid_card():
    from ticketlab.adapters.billing import BillingAdapter, Invoice
    b = BillingAdapter()
    b.payment_method["status"] = "expired"
    b.invoices["x"] = Invoice(id="x", amount=100)
    assert b.retry_payment("x") is False
    assert b.invoices["x"].status == "pending"
