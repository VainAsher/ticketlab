"""Trainer journey: monitoring endpoints, identity attribution from the
Authentik forward-auth header, and the authoring surface (vocab -> validate
-> publish -> playable, persisted, builtin-protected).
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    from ticketlab.api import create_app
    app = create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db")
    return TestClient(app)


def _draft(sid="authored-smoke"):
    """Minimal valid scenario a trainer could produce with the wizard."""
    return {
        "schema_version": 2,
        "metadata": {
            "id": sid, "title": "Authored smoke", "version": "1.0.0",
            "author": "trainer",
            "provenance": {"created": "2026-07-06", "source": "generic"},
            "difficulty": 1, "estimated_minutes": 5,
            "objectives": ["Get the server running again"],
        },
        "panel": {"adapter": "mock"},
        "ticket": {"subject": "server down", "priority": "low",
                   "customer": {"name": "Pat", "persona": "frustrated-novice"},
                   "body": "it is down"},
        "environment": {"server": {"name": "pat-smp", "egg": "minecraft-paper",
                                   "limits": {"memory": 1024},
                                   "variables": {}}},
        "fault": {"steps": [{"action": "stop_server"}]},
        "conversation": {"persona": "frustrated-novice",
                         "satisfaction_start": 50,
                         "hidden_facts": [
                             {"id": "why", "fact": "turned it off",
                              "reveal_keywords": ["chang"]}]},
        "verification": {"solutions": [
            {"id": "start-it", "grade": "full", "score": 100,
             "label": "Started the server",
             "assertions": [{"type": "server_state", "operator": "equals",
                             "expected": "running"}]}]},
    }


def test_attempt_attributed_from_authentik_header(client):
    r = client.post("/attempts", json={"scenario_id": "suspended-not-broken"},
                    headers={"X-Authentik-Username": "lena.trainee"})
    assert r.status_code == 201
    rows = client.get("/trainer/attempts").json()
    assert rows[0]["trainee"] == "lena.trainee"
    assert rows[0]["scenario_id"] == "suspended-not-broken"


def test_attempt_anonymous_without_header(client):
    client.post("/attempts", json={"scenario_id": "suspended-not-broken"})
    assert client.get("/trainer/attempts").json()[0]["trainee"] == "anonymous"


def test_trainer_listing_filters_and_grade_status(client):
    for sid in ("suspended-not-broken", "jar-rename-typo"):
        client.post("/attempts", json={"scenario_id": sid},
                    headers={"X-Authentik-Username": "sam"})
    rows = client.get("/trainer/attempts",
                      params={"scenario_id": "jar-rename-typo"}).json()
    assert len(rows) == 1 and rows[0]["grades_status"] is None


def test_trainer_attempt_detail_has_timeline(client):
    aid = client.post("/attempts",
                      json={"scenario_id": "suspended-not-broken"}).json()["attempt_id"]
    client.post(f"/attempts/{aid}/message", json={"text": "did a payment fail?"})
    d = client.get(f"/trainer/attempts/{aid}").json()
    assert d["record"]["id"] == aid
    assert d["events"][0]["kind"] == "message"
    assert d["events"][0]["payload"]["facts_revealed"] == ["card-expired"]


def test_vocab_covers_whitelists(client):
    v = client.get("/authoring/vocab").json()
    assert "suspend_server" in v["fault_verbs"]
    assert v["fault_verbs"]["set_variable"]["fields"] == ["key", "value"]
    assert "activity_occurred" in v["assertion_types"]
    assert v["grades"] == ["temp", "partial", "full"]
    assert "frustrated-novice" in v["personas"]


def test_validate_flags_bad_verb_with_field_path(client):
    d = _draft()
    d["fault"]["steps"][0]["action"] = "explode_server"
    r = client.post("/authoring/validate", json={"scenario": d}).json()
    assert r["valid"] is False
    assert any("fault.steps" in e["loc"] for e in r["errors"])


def test_validate_rejects_builtin_id_collision(client):
    r = client.post("/authoring/validate",
                    json={"scenario": _draft("suspended-not-broken")}).json()
    assert r["valid"] is False
    assert any(e["loc"] == "metadata.id" for e in r["errors"])


def test_publish_then_play_then_survives_restart(client, tmp_path):
    r = client.post("/authoring/scenarios", json={"scenario": _draft()})
    assert r.status_code == 201, r.text
    # live immediately: listed and playable
    assert any(s["id"] == "authored-smoke"
               for s in client.get("/scenarios").json())
    aid = client.post("/attempts",
                      json={"scenario_id": "authored-smoke"}).json()["attempt_id"]
    client.post(f"/attempts/{aid}/demo/mutate", json={"action": "start"})
    assert client.post(f"/attempts/{aid}/verify").json()["complete"] is True
    # persisted next to the DB: a fresh app over the same volume still has it
    from ticketlab.api import create_app
    app2 = create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db")
    assert any(s["id"] == "authored-smoke"
               for s in TestClient(app2).get("/scenarios").json())
    origin = {s["id"]: s["origin"]
              for s in TestClient(app2).get("/trainer/scenarios").json()}
    assert origin["authored-smoke"] == "authored"
    assert origin["suspended-not-broken"] == "builtin"


def test_publish_refuses_builtin_overwrite(client):
    r = client.post("/authoring/scenarios",
                    json={"scenario": _draft("suspended-not-broken")})
    assert r.status_code == 409
