"""Phase 6: orchestrator + FastAPI, end-to-end via TestClient. Written first.

Full arc: create attempt -> diagnostic chat reveals fact -> demo temp-fix ->
verify (temp grade) -> demo root-cause fix -> verify (full) -> debrief shows
combined technical + conversational picture. Plus: verify-attempt budget,
reinstall triggers angry customer, escalation ends conversation.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    from ticketlab.api import create_app
    app = create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db")
    return TestClient(app)


def start(client):
    r = client.post("/attempts", json={"scenario_id": "oom-crash-loop-modded"})
    assert r.status_code == 201, r.text
    return r.json()["attempt_id"]


def test_attempt_creation_returns_ticket_not_solution(client):
    r = client.post("/attempts", json={"scenario_id": "oom-crash-loop-modded"})
    body = r.json()
    assert "restarts" in body["ticket"]["body"]
    dumped = str(body)
    assert "Xmx4096" not in dumped          # fault details never leak to trainee
    assert "right-size-heap" not in dumped  # solutions never leak


def test_unknown_scenario_404(client):
    r = client.post("/attempts", json={"scenario_id": "nope"})
    assert r.status_code == 404


def test_chat_reveals_fact_on_good_diagnostic(client):
    aid = start(client)
    r = client.post(f"/attempts/{aid}/message",
                    json={"text": "Anything changed recently, new mods installed?"})
    assert r.status_code == 200
    assert "added-mods" in r.json()["facts_revealed"]


def test_verify_while_broken_incomplete_and_budgeted(client):
    aid = start(client)
    for i in range(5):
        r = client.post(f"/attempts/{aid}/verify")
        assert r.json()["complete"] is False
    r = client.post(f"/attempts/{aid}/verify")
    assert r.status_code == 429            # max_verify_attempts exhausted


def test_full_arc_temp_then_root_cause(client):
    aid = start(client)
    # temp fix via demo controls
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_limits", "memory": 5120})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    r = client.post(f"/attempts/{aid}/verify").json()
    assert r["complete"] and r["grade"] == "temp" and r["score"] == 55
    # then root cause
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_startup_command",
                      "value": "java -Xms512M -Xmx1024M -jar server.jar"})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "restart"})
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    r = client.post(f"/attempts/{aid}/verify").json()
    assert r["grade"] == "full" and r["score"] == 100


def test_reinstall_makes_customer_angry_and_penalises(client):
    aid = start(client)
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "reinstall"})
    r = client.post(f"/attempts/{aid}/message",
                    json={"text": "I've reinstalled the server for you!"})
    body = r.json()
    assert body["satisfaction_delta"] < 0
    assert "gone" in body["reply"].lower() or "wiped" in body["reply"].lower()
    # fix it properly; penalty still applies (historical)
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_startup_command",
                      "value": "java -Xmx1024M -jar server.jar"})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    r = client.post(f"/attempts/{aid}/verify").json()
    assert r["score"] == 70 and "reinstalled" in r["anti_patterns_hit"]


def test_debrief_combines_both_planes(client):
    aid = start(client)
    client.post(f"/attempts/{aid}/message", json={"text": "any errors in your console?"})
    client.post(f"/attempts/{aid}/demo/mutate",
                json={"action": "set_startup_command",
                      "value": "java -Xmx1024M -jar server.jar"})
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})
    client.post(f"/attempts/{aid}/demo/advance_clock", json={"seconds": 181})
    client.post(f"/attempts/{aid}/verify")
    r = client.get(f"/attempts/{aid}/debrief").json()
    assert r["technical"]["grade"] == "full"
    assert r["conversation"]["facts_uncovered"] == 1
    assert r["conversation"]["facts_total"] == 2
    assert 0 <= r["conversation"]["satisfaction_final"] <= 100
    assert r["verify_attempts_used"] >= 1


def test_attempt_creation_includes_trainee_safe_brief(client):
    body = client.post("/attempts",
                       json={"scenario_id": "oom-crash-loop-modded"}).json()
    sc = body["scenario"]
    assert sc["facts_total"] == 2          # count only — never fact content
    assert sc["objectives"]                # defaults when scenario authors none
    dumped = str(body)
    assert "Xmx4096" not in dumped         # brief must pass the same firewall
    assert "right-size-heap" not in dumped


def test_authored_objectives_served_and_leak_free(client):
    body = client.post("/attempts",
                       json={"scenario_id": "bad-jvm-flag-wont-start"}).json()
    sc = body["scenario"]
    assert any("changed before the outage" in o for o in sc["objectives"])
    dumped = str(body)
    assert "UseSuperSpeed" not in dumped   # fault detail
    assert "remove-bad-flags" not in dumped  # solution id


def test_message_length_capped(client):
    aid = start(client)
    r = client.post(f"/attempts/{aid}/message", json={"text": "x" * 100_000})
    assert r.status_code in (200, 422)   # either rejected or truncated-and-handled
