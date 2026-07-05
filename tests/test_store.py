"""C5: SQLite attempt store. Written before ticketlab/store.py.

Invariants:
- attempt records survive a store reopen (the whole point: analytics outlive restarts)
- event log is append-only and ordered
- finalize is idempotent (best result wins, no duplicate rows)
- per-scenario analytics aggregate correctly
- live orchestrator writes through: create/message/verify/debrief all leave rows
"""
from pathlib import Path

SCEN = Path(__file__).parent.parent / "scenarios" / "oom-crash-loop-modded.yaml"


def test_record_survives_reopen(tmp_path):
    from ticketlab.store import AttemptStore
    db = tmp_path / "lab.db"
    s1 = AttemptStore(db)
    s1.start_attempt("abc123", "oom-crash-loop-modded")
    s1.log_event("abc123", "message", {"text": "hi"}, ts=1.0)
    s1.finalize("abc123", {"technical": {"grade": "full", "score": 100,
                                         "complete": True, "anti_patterns": []},
                           "conversation": {"satisfaction_final": 60,
                                            "escalated": False,
                                            "facts_uncovered": 1,
                                            "facts_total": 2, "turns": 3},
                           "verify_attempts_used": 2})
    s1.close()
    s2 = AttemptStore(db)
    rec = s2.get_record("abc123")
    assert rec["grade"] == "full" and rec["score"] == 100
    assert rec["facts_uncovered"] == 1
    assert len(s2.events("abc123")) == 1


def test_events_append_only_and_ordered(tmp_path):
    from ticketlab.store import AttemptStore
    s = AttemptStore(tmp_path / "lab.db")
    s.start_attempt("a1", "x")
    for i in range(3):
        s.log_event("a1", "verify", {"n": i}, ts=float(i))
    evs = s.events("a1")
    assert [e["payload"]["n"] for e in evs] == [0, 1, 2]


def test_finalize_idempotent_best_wins(tmp_path):
    from ticketlab.store import AttemptStore
    s = AttemptStore(tmp_path / "lab.db")
    s.start_attempt("a1", "x")
    base = {"conversation": {"satisfaction_final": 50, "escalated": False,
                             "facts_uncovered": 0, "facts_total": 2, "turns": 1},
            "verify_attempts_used": 1}
    s.finalize("a1", {**base, "technical": {"grade": "temp", "score": 55,
                                            "complete": True, "anti_patterns": []}})
    s.finalize("a1", {**base, "technical": {"grade": "full", "score": 100,
                                            "complete": True, "anti_patterns": []}})
    rec = s.get_record("a1")
    assert rec["score"] == 100
    assert s.count_records() == 1


def test_analytics_summary_aggregates(tmp_path):
    from ticketlab.store import AttemptStore
    s = AttemptStore(tmp_path / "lab.db")
    conv = {"satisfaction_final": 50, "escalated": False,
            "facts_uncovered": 1, "facts_total": 2, "turns": 2}
    for i, (grade, score) in enumerate([("full", 100), ("temp", 55), (None, 0)]):
        aid = f"a{i}"
        s.start_attempt(aid, "oom-crash-loop-modded")
        s.finalize(aid, {"technical": {"grade": grade, "score": score,
                                       "complete": grade is not None,
                                       "anti_patterns": []},
                         "conversation": conv, "verify_attempts_used": 3})
    summ = s.summary()["oom-crash-loop-modded"]
    assert summ["attempts"] == 3
    assert summ["completed"] == 2
    assert summ["avg_score"] == (100 + 55 + 0) / 3
    assert summ["grades"] == {"full": 1, "temp": 1}


def test_orchestrator_writes_through(tmp_path):
    from fastapi.testclient import TestClient
    from ticketlab.api import create_app
    from ticketlab.store import AttemptStore
    db = tmp_path / "lab.db"
    c = TestClient(create_app(scenario_dir="scenarios", db_path=db))
    aid = c.post("/attempts", json={"scenario_id": "oom-crash-loop-modded"}).json()["attempt_id"]
    c.post(f"/attempts/{aid}/message", json={"text": "any console errors?"})
    c.post(f"/attempts/{aid}/verify")
    c.get(f"/attempts/{aid}/debrief")
    store = AttemptStore(db)
    rec = store.get_record(aid)
    assert rec is not None and rec["scenario_id"] == "oom-crash-loop-modded"
    kinds = [e["kind"] for e in store.events(aid)]
    assert "message" in kinds and "verify" in kinds


def test_analytics_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from ticketlab.api import create_app
    c = TestClient(create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db"))
    aid = c.post("/attempts", json={"scenario_id": "oom-crash-loop-modded"}).json()["attempt_id"]
    c.get(f"/attempts/{aid}/debrief")
    r = c.get("/analytics/summary")
    assert r.status_code == 200
    assert "oom-crash-loop-modded" in r.json()
