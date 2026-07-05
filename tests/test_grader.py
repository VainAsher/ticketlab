"""C2: reply-quality grading, propose-not-decide. Written before grader.py.

Invariants:
- grades are ALWAYS status='proposed' until a trainer confirms — the AI never
  decides; verified at the API layer
- premature promise detected from the event timeline (claimed fixed before
  any complete verify event)
- keyword-salad detected (fact-mining without writing a real reply)
- jargon flagged against a non-technical persona
- Ollama grader failure falls back to the heuristic grader, silently
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SCEN_DIR = Path(__file__).parent.parent / "scenarios"


def events_fixture(premature=False, salad=False, jargon=False, empathy=True):
    """Synthetic event timeline shaped exactly like AttemptStore.events()."""
    evs = []
    first = ("So sorry about this — that's rough timing. Could you tell me "
             "if anything changed recently?") if empathy else \
            ("send me your server id.")
    evs.append({"ts": 1.0, "kind": "message",
                "payload": {"text": first, "facts_revealed": ["added-mods"],
                            "satisfaction": 45, "state_events": [],
                            "escalated": False}})
    if salad:
        evs.append({"ts": 2.0, "kind": "message",
                    "payload": {"text": "changed installed mods console error log",
                                "facts_revealed": ["ignored-warning"],
                                "satisfaction": 50, "state_events": [],
                                "escalated": False}})
    if premature:
        evs.append({"ts": 3.0, "kind": "message",
                    "payload": {"text": "All fixed! Should be working now.",
                                "facts_revealed": [], "satisfaction": 55,
                                "state_events": [], "escalated": False}})
    if jargon:
        evs.append({"ts": 4.0, "kind": "message",
                    "payload": {"text": "Your JVM heap Xmx exceeded the container "
                                        "cgroup limit so the kernel OOM-killed it.",
                                "facts_revealed": [], "satisfaction": 55,
                                "state_events": [], "escalated": False}})
    evs.append({"ts": 5.0, "kind": "verify",
                "payload": {"complete": True, "grade": "full", "score": 100,
                            "anti_patterns": []}})
    return evs


def load(sid="oom-crash-loop-modded"):
    from ticketlab.schema import load_scenario
    return load_scenario(SCEN_DIR / f"{sid}.yaml")


def test_clean_transcript_no_flags():
    from ticketlab.grader import HeuristicGrader
    g = HeuristicGrader().grade(events_fixture(), load())
    assert g["flags"] == []
    names = {d["name"] for d in g["dimensions"]}
    assert {"empathy", "diagnosis", "integrity", "register"} <= names


def test_premature_promise_flagged():
    from ticketlab.grader import HeuristicGrader
    g = HeuristicGrader().grade(events_fixture(premature=True), load())
    assert any(f["id"] == "premature_promise" for f in g["flags"])
    integrity = next(d for d in g["dimensions"] if d["name"] == "integrity")
    assert integrity["score"] < 5


def test_fix_claim_after_verify_not_flagged():
    from ticketlab.grader import HeuristicGrader
    evs = events_fixture()
    evs.append({"ts": 6.0, "kind": "message",
                "payload": {"text": "That's fixed now — verified it's stable.",
                            "facts_revealed": [], "satisfaction": 70,
                            "state_events": [], "escalated": False}})
    g = HeuristicGrader().grade(evs, load())
    assert not any(f["id"] == "premature_promise" for f in g["flags"])


def test_keyword_salad_flagged():
    from ticketlab.grader import HeuristicGrader
    g = HeuristicGrader().grade(events_fixture(salad=True), load())
    assert any(f["id"] == "keyword_salad" for f in g["flags"])


def test_jargon_flagged_for_novice_persona_only():
    from ticketlab.grader import HeuristicGrader
    scen_novice = load("bad-jvm-flag-wont-start")     # frustrated-novice
    scen_tech = load("wrong-java-version")            # technical-but-wrong
    g1 = HeuristicGrader().grade(events_fixture(jargon=True), scen_novice)
    g2 = HeuristicGrader().grade(events_fixture(jargon=True), scen_tech)
    assert any(f["id"] == "jargon_mismatch" for f in g1["flags"])
    assert not any(f["id"] == "jargon_mismatch" for f in g2["flags"])


def test_ollama_grader_falls_back_to_heuristic():
    from ticketlab.grader import GraderRunner

    class Exploding:
        name = "ollama"
        def grade(self, events, scenario):
            raise ConnectionError("down")

    out = GraderRunner(primary=Exploding()).run(events_fixture(), load())
    assert out["grader"] == "heuristic"
    assert out["grades"]["dimensions"]


@pytest.fixture()
def client(tmp_path):
    from ticketlab.api import create_app
    return TestClient(create_app(scenario_dir="scenarios",
                                 db_path=tmp_path / "lab.db"))


def _run_attempt(client):
    aid = client.post("/attempts",
                      json={"scenario_id": "oom-crash-loop-modded"}).json()["attempt_id"]
    client.post(f"/attempts/{aid}/message", json={"text": "any console errors?"})
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_startup_command",
                      "value": "java -Xmx1024M -jar server.jar"})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    client.post(f"/attempts/{aid}/verify")
    return aid


def test_grades_endpoint_proposes_never_decides(client):
    aid = _run_attempt(client)
    r = client.get(f"/attempts/{aid}/grades").json()
    assert r["status"] == "proposed"
    # idempotent: second GET returns the stored proposal, still proposed
    assert client.get(f"/attempts/{aid}/grades").json()["status"] == "proposed"


def test_trainer_confirmation_persists(client):
    aid = _run_attempt(client)
    client.get(f"/attempts/{aid}/grades")
    r = client.post(f"/attempts/{aid}/grades/confirm",
                    json={"confirmed_by": "vain",
                          "overrides": {"empathy": 9}})
    assert r.status_code == 200
    g = client.get(f"/attempts/{aid}/grades").json()
    assert g["status"] == "confirmed" and g["confirmed_by"] == "vain"
    emp = next(d for d in g["grades"]["dimensions"] if d["name"] == "empathy")
    assert emp["score"] == 9 and emp.get("overridden") is True


def test_confirm_before_proposal_404(client):
    aid = _run_attempt(client)
    r = client.post(f"/attempts/{aid}/grades/confirm", json={"confirmed_by": "vain"})
    assert r.status_code == 404
