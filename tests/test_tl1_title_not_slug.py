"""TL-1 (2026-07-08 review): the trainer attempt listing/detail surfaced the
scenario SLUG (scenario_id) where a human-readable TITLE should appear.

The trainee-facing /scenarios listing already carries metadata.title; the gap
is the trainer surface: /trainer/attempts rows and /trainer/attempts/{id}
records came straight from the store with only scenario_id, and trainer.html
rendered that slug raw. These tests pin the enriched responses.

Note on the fallback case: metadata.title is REQUIRED by the schema, so no
*loaded* scenario can lack a title. The real no-title case the data model
supports is an ORPHANED attempt row — authored scenarios live next to the db
and their yaml can be deleted after attempts were recorded. That row must
still render something readable, not the raw slug.
"""
import pytest
from fastapi.testclient import TestClient

BAD_JVM_SLUG = "bad-jvm-flag-wont-start"
BAD_JVM_TITLE = "Server won't start — copied 'optimisation' flags"


@pytest.fixture()
def client(tmp_path):
    # ISOLATION: db_path MUST be under tmp_path — the app derives its
    # authored-scenarios dir from db_path.parent, so this keeps every write
    # away from the real ticketlab.db (standing rule from past incidents).
    from ticketlab.api import create_app
    app = create_app(scenario_dir="scenarios", db_path=tmp_path / "lab.db")
    return TestClient(app)


def _start(client, scenario_id=BAD_JVM_SLUG):
    r = client.post("/attempts", json={"scenario_id": scenario_id})
    assert r.status_code == 201, r.text
    return r.json()["attempt_id"]


def test_trainer_attempts_listing_carries_title_not_just_slug(client):
    _start(client)
    rows = client.get("/trainer/attempts").json()
    assert rows, "attempt should be listed"
    row = rows[0]
    assert row["scenario_id"] == BAD_JVM_SLUG      # analytics key stays intact
    assert row["scenario_title"] == BAD_JVM_TITLE  # human-readable field added


def test_trainer_attempt_detail_carries_title(client):
    aid = _start(client)
    rec = client.get(f"/trainer/attempts/{aid}").json()["record"]
    assert rec["scenario_title"] == BAD_JVM_TITLE


def test_orphaned_attempt_gets_readable_fallback_not_raw_slug(client):
    # Simulate an attempt whose (authored) scenario yaml no longer exists:
    # the row survives in the store, the scenarios dict has no entry for it.
    client.app.state.store.start_attempt("att-orphan", "deleted-authored-scenario")
    rows = client.get("/trainer/attempts").json()
    row = next(r for r in rows if r["id"] == "att-orphan")
    assert row["scenario_title"] == "Deleted authored scenario"
