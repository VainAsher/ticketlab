"""AttemptStore — SQLite persistence for attempt records, event logs, and
grades. Stdlib sqlite3 only; the analytics that outlive a restart are the
internal case study ("did training reduce reopens?" needs numbers).

Live attempts (adapters, engines) stay in memory — this stores the RECORD:
an append-only event timeline plus a finalized summary row. finalize() is
idempotent with best-score-wins so repeated debrief calls don't duplicate.

The grades table backs C2: proposals land as status='proposed'; a trainer
confirmation updates to 'confirmed'. Propose-not-decide, enforced by schema.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  complete INTEGER,
  grade TEXT,
  score INTEGER,
  satisfaction_final INTEGER,
  escalated INTEGER,
  facts_uncovered INTEGER,
  facts_total INTEGER,
  turns INTEGER,
  verify_attempts INTEGER,
  anti_patterns TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON events(attempt_id, seq);
CREATE TABLE IF NOT EXISTS grades (
  attempt_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('proposed','confirmed')),
  grader TEXT NOT NULL,
  payload TEXT NOT NULL,
  confirmed_by TEXT,
  updated_at REAL NOT NULL
);
"""


class AttemptStore:
    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── attempts ──
    def start_attempt(self, attempt_id: str, scenario_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO attempts (id, scenario_id, started_at) "
            "VALUES (?,?,?)", (attempt_id, scenario_id, time.time()))
        self._conn.commit()

    def finalize(self, attempt_id: str, debrief: dict) -> None:
        t, c = debrief["technical"], debrief["conversation"]
        row = self._conn.execute(
            "SELECT score FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row and row["score"] is not None and row["score"] >= (t.get("score") or 0):
            return  # idempotent: best result already recorded
        self._conn.execute(
            """UPDATE attempts SET finished_at=?, complete=?, grade=?, score=?,
               satisfaction_final=?, escalated=?, facts_uncovered=?,
               facts_total=?, turns=?, verify_attempts=?, anti_patterns=?
               WHERE id=?""",
            (time.time(), int(bool(t.get("complete"))), t.get("grade"),
             t.get("score") or 0, c.get("satisfaction_final"),
             int(bool(c.get("escalated"))), c.get("facts_uncovered"),
             c.get("facts_total"), c.get("turns"),
             debrief.get("verify_attempts_used"),
             json.dumps(t.get("anti_patterns") or []), attempt_id))
        self._conn.commit()

    def get_record(self, attempt_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["anti_patterns"] = json.loads(d["anti_patterns"] or "[]")
        return d

    def count_records(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    # ── events (append-only) ──
    def log_event(self, attempt_id: str, kind: str, payload: dict,
                  ts: float) -> None:
        self._conn.execute(
            "INSERT INTO events (attempt_id, ts, kind, payload) VALUES (?,?,?,?)",
            (attempt_id, ts, kind, json.dumps(payload)))
        self._conn.commit()

    def events(self, attempt_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, kind, payload FROM events WHERE attempt_id=? "
            "ORDER BY seq", (attempt_id,)).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"],
                 "payload": json.loads(r["payload"])} for r in rows]

    # ── grades (C2: propose-not-decide) ──
    def save_grades(self, attempt_id: str, payload: dict, grader: str,
                    status: str = "proposed",
                    confirmed_by: str | None = None) -> None:
        self._conn.execute(
            """INSERT INTO grades (attempt_id, status, grader, payload,
               confirmed_by, updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status,
               grader=excluded.grader, payload=excluded.payload,
               confirmed_by=excluded.confirmed_by,
               updated_at=excluded.updated_at""",
            (attempt_id, status, grader, json.dumps(payload),
             confirmed_by, time.time()))
        self._conn.commit()

    def get_grades(self, attempt_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM grades WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            return None
        return {"status": row["status"], "grader": row["grader"],
                "confirmed_by": row["confirmed_by"],
                "grades": json.loads(row["payload"])}

    # ── analytics ──
    def summary(self) -> dict:
        rows = self._conn.execute(
            """SELECT scenario_id, COUNT(*) AS attempts,
               SUM(COALESCE(complete,0)) AS completed,
               AVG(COALESCE(score,0)) AS avg_score,
               AVG(COALESCE(satisfaction_final,0)) AS avg_satisfaction,
               SUM(COALESCE(escalated,0)) AS escalations
               FROM attempts GROUP BY scenario_id""").fetchall()
        out: dict = {}
        for r in rows:
            grades = self._conn.execute(
                "SELECT grade, COUNT(*) n FROM attempts WHERE scenario_id=? "
                "AND grade IS NOT NULL GROUP BY grade",
                (r["scenario_id"],)).fetchall()
            out[r["scenario_id"]] = {
                "attempts": r["attempts"], "completed": r["completed"],
                "avg_score": r["avg_score"],
                "avg_satisfaction": r["avg_satisfaction"],
                "escalations": r["escalations"],
                "grades": {g["grade"]: g["n"] for g in grades},
            }
        return out
