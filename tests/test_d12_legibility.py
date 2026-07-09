"""D12 — trainee loop legibility (spec: D12 design session 2026-07-09).

Four user rulings, pinned as contracts:
1. hint_level default 'nudge' — failed verifies carry a type-keyed, engine-level
   hint string that never names the target; 'none' restores silence; 'explicit'
   names the assertion with live progress.
2. Sim clock counts — the attempt clock accrues wall time 1:1 AND advance_clock
   jumps it, so waiting in-sim satisfies stable_for_seconds. Both paths pinned:
   wait-then-verify passes, instant re-verify still fails.
3. Full reveal at debrief — an unresolved debrief lists unmet assertions per
   solution plus the closest solution's authored feedback. Mid-attempt secrecy
   stays hint_level's job.
4. (Confirm-on-final is client-side; the server contract it rides on —
   attempts_remaining in every verify response — is pinned in test_api.)
"""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ticketlab.schema import load_scenario_from_dict
from ticketlab.orchestrator import Orchestrator

# The five fixed engine strings (fixed = non-spoiler by construction: nothing
# scenario-specific goes in, so nothing scenario-specific can leak).
NUDGE_STABILITY = ("Your fix may need time to prove itself — wait a little "
                   "before re-verifying.")
NUDGE_STATE = "The server isn't in the state the customer needs yet."
NUDGE_BILLING = "The underlying account or billing issue isn't cleared yet."
NUDGE_ACTIVITY = "Something the fix requires hasn't been done yet."
NUDGE_CONFIG = "The server's configuration isn't what the fix needs yet."
ALL_NUDGES = {NUDGE_STABILITY, NUDGE_STATE, NUDGE_BILLING, NUDGE_ACTIVITY,
              NUDGE_CONFIG}


@pytest.fixture()
def client(tmp_path):
    from ticketlab.api import create_app
    app = create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db")
    return TestClient(app)


def start(client, scenario_id="oom-crash-loop-modded"):
    r = client.post("/attempts", json={"scenario_id": scenario_id})
    assert r.status_code == 201, r.text
    return r.json()["attempt_id"]


def temp_fix(client, aid):
    """The oom scenario's temp path: raise the limit, start the server."""
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_limits", "memory": 5120})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})


def load_raw(scenario="oom-crash-loop-modded"):
    return yaml.safe_load(
        Path(f"scenarios/{scenario}.yaml").read_text(encoding="utf-8"))


# ── ruling 1: hint_level ──

def test_hint_level_defaults_to_nudge():
    s = load_scenario_from_dict(load_raw())   # yaml carries no hint_level
    assert s.verification.hint_level == "nudge"


def test_hint_level_rejects_unknown_value():
    raw = load_raw()
    raw["verification"]["hint_level"] = "spoiler"
    with pytest.raises(Exception):
        load_scenario_from_dict(raw)


def test_failed_verify_carries_nudge_by_default(client):
    aid = start(client)
    r = client.post(f"/attempts/{aid}/verify").json()
    assert r["complete"] is False
    assert r["hint"] in ALL_NUDGES
    # non-spoiler by construction: no scenario content in the hint
    for spoiler in ("Xmx", "heap", "4096", "1024", "right-size"):
        assert spoiler not in r["hint"]


def test_stability_pending_nudges_wait(client):
    """The repro run's missing sentence: fix applied and holding, only the
    stability window unmet -> the nudge says to wait, not to keep digging."""
    aid = start(client)
    temp_fix(client, aid)
    r = client.post(f"/attempts/{aid}/verify").json()   # instant re-verify
    assert r["complete"] is False
    assert r["hint"] == NUDGE_STABILITY


def test_hint_level_none_stays_silent():
    raw = load_raw()
    raw["verification"]["hint_level"] = "none"
    orch = Orchestrator()
    a = orch.create_attempt(load_scenario_from_dict(raw))
    r = orch.verify(a)
    assert r.complete is False and r.hint == ""


def test_hint_level_explicit_names_assertion_with_progress():
    raw = load_raw()
    raw["verification"]["hint_level"] = "explicit"
    orch = Orchestrator()
    a = orch.create_attempt(load_scenario_from_dict(raw))
    a.adapter.set_limits(memory=5120)
    a.adapter.set_power_state("running")
    r = orch.verify(a)
    assert r.complete is False
    assert "180s" in r.hint          # names the window...
    assert "held" in r.hint          # ...and reports live progress


def test_success_carries_no_hint(client):
    aid = start(client)
    temp_fix(client, aid)
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    r = client.post(f"/attempts/{aid}/verify").json()
    assert r["complete"] is True and r["hint"] == ""


def test_authoring_vocab_offers_hint_levels(client):
    v = client.get("/authoring/vocab").json()
    assert v["hint_levels"] == ["none", "nudge", "explicit"]


# ── ruling 2: sim clock ──

def test_sim_clock_accrues_wall_time_and_advances(monkeypatch):
    from ticketlab.adapters import mock as mock_mod
    wall = {"t": 500.0}
    monkeypatch.setattr(mock_mod.time, "monotonic", lambda: wall["t"])
    clock = mock_mod.SimClock(start=0.0)
    assert clock.now() == 0.0
    wall["t"] = 560.0
    assert clock.now() == 60.0        # doing nothing also works (1:1 accrual)
    clock.advance(120)
    assert clock.now() == 180.0       # the Wait button stacks on top


def test_attempts_run_on_the_sim_clock():
    from ticketlab.adapters.mock import SimClock
    orch = Orchestrator()
    a = orch.create_attempt(load_scenario_from_dict(load_raw()))
    assert isinstance(a.clock, SimClock)


def test_wait_then_verify_passes_instant_reverify_fails(client):
    aid = start(client)
    temp_fix(client, aid)
    r1 = client.post(f"/attempts/{aid}/verify").json()
    assert r1["complete"] is False                      # instant: window unmet
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    r2 = client.post(f"/attempts/{aid}/verify").json()
    assert r2["complete"] is True and r2["grade"] == "temp"


# ── ruling 3: full reveal at debrief ──

def test_unresolved_debrief_reveals_unmet_and_closest_feedback(client):
    aid = start(client)
    for _ in range(5):                                  # burn the whole budget
        client.post(f"/attempts/{aid}/verify")
    assert client.post(f"/attempts/{aid}/verify").status_code == 429
    tech = client.get(f"/attempts/{aid}/debrief").json()["technical"]
    assert tech["complete"] is False
    labels = [u for sol in tech["unmet"] for u in sol["unmet"]]
    assert labels, "unresolved debrief must name the unmet assertions"
    assert any("startup command" in lbl for lbl in labels)
    # closest solution on the untouched fault is the full fix — its authored
    # feedback (the teaching moment every YAML already carries) is revealed
    assert "Root cause" in tech["closest_feedback"]


def test_abandoned_attempt_debrief_still_reveals(client):
    aid = start(client)                                 # zero verifies, quit
    tech = client.get(f"/attempts/{aid}/debrief").json()["technical"]
    assert tech["unmet"] and tech["closest_feedback"]


def test_resolved_debrief_has_no_unmet_noise(client):
    aid = start(client)
    temp_fix(client, aid)
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    assert client.post(f"/attempts/{aid}/verify").json()["complete"] is True
    tech = client.get(f"/attempts/{aid}/debrief").json()["technical"]
    assert tech["unmet"] == [] and tech["closest_feedback"] == ""
