"""FastAPI surface. Trainee-facing responses NEVER include fault or solution
content — the response models are explicit allowlists, not filtered dumps.

Demo endpoints simulate trainee panel actions against the MockAdapter so the
whole loop can be presented with no Pterodactyl instance in the room. In
panel mode (Phase 2 of rollout) these endpoints are disabled and the trainee
acts in the real iframed panel instead.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ticketlab.schema import load_scenario, Scenario
from ticketlab.orchestrator import Orchestrator
from ticketlab.store import AttemptStore
from ticketlab.grader import GraderRunner


# ── request/response models (allowlists) ──
class CreateAttemptReq(BaseModel):
    scenario_id: str


# Served when a scenario authors no objectives of its own. Deliberately
# generic: goals describe what success looks like, never how to get there.
DEFAULT_OBJECTIVES = [
    "Find out what the customer isn't volunteering",
    "Restore the server to a stable running state",
    "Keep the customer on side — don't let it escalate",
    "Verify your fix before the attempt budget runs out",
]


class MessageReq(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class MutateReq(BaseModel):
    action: str
    key: str | None = Field(default=None, max_length=100)
    value: str | None = Field(default=None, max_length=500)
    memory: int | None = Field(default=None, ge=0, le=1_048_576)


class AdvanceClockReq(BaseModel):
    seconds: int = Field(ge=1, le=86_400)


class ConfirmGradesReq(BaseModel):
    confirmed_by: str = Field(min_length=1, max_length=100)
    overrides: dict[str, int] = Field(default_factory=dict)


def create_app(scenario_dir: str = "scenarios", llm=None,
               demo_mode: bool = True, grader=None,
               db_path: str | Path = "ticketlab.db") -> FastAPI:
    app = FastAPI(title="TicketLab", version="0.2.0")
    store = AttemptStore(db_path)
    orch = Orchestrator(llm=llm, store=store)
    grader_runner = GraderRunner(primary=grader)
    app.state.store = store

    scenarios: dict[str, Scenario] = {}
    for p in sorted(Path(scenario_dir).glob("*.yaml")):
        s = load_scenario(p)
        scenarios[s.metadata.id] = s

    @app.get("/scenarios")
    def list_scenarios():
        return [{"id": s.metadata.id, "title": s.metadata.title,
                 "difficulty": s.metadata.difficulty,
                 "estimated_minutes": s.metadata.estimated_minutes}
                for s in scenarios.values()]

    @app.post("/attempts", status_code=201)
    def create_attempt(req: CreateAttemptReq):
        s = scenarios.get(req.scenario_id)
        if s is None:
            raise HTTPException(404, "unknown scenario")
        a = orch.create_attempt(s)
        return {
            "attempt_id": a.id,
            "ticket": {  # explicit allowlist — never model_dump the scenario
                "subject": s.ticket.subject,
                "priority": s.ticket.priority,
                "customer_name": s.ticket.customer.name,
                "body": s.ticket.body,
            },
            "satisfaction": a.conversation.state.satisfaction,
            "verify_budget": s.scoring.max_verify_attempts,
            "scenario": {  # trainee-safe brief — counts and goals, never content
                "title": s.metadata.title,
                "difficulty": s.metadata.difficulty,
                "estimated_minutes": s.metadata.estimated_minutes,
                "objectives": s.metadata.objectives or DEFAULT_OBJECTIVES,
                "facts_total": len(s.conversation.hidden_facts),
            },
        }

    def _get(attempt_id: str):
        a = orch.get(attempt_id)
        if a is None:
            raise HTTPException(404, "unknown attempt")
        return a

    @app.post("/attempts/{attempt_id}/message")
    def message(attempt_id: str, req: MessageReq):
        a = _get(attempt_id)
        t = orch.trainee_message(a, req.text)
        return {"reply": t.reply, "facts_revealed": t.facts_revealed,
                "satisfaction": t.satisfaction,
                "satisfaction_delta": t.satisfaction_delta,
                "escalated": t.escalated}

    @app.post("/attempts/{attempt_id}/verify")
    def verify(attempt_id: str):
        a = _get(attempt_id)
        r = orch.verify(a)
        if r is None:
            raise HTTPException(429, "verify attempt budget exhausted")
        return {"complete": r.complete, "grade": r.grade, "score": r.score,
                "solution_label": r.matched_solution,
                "feedback": r.feedback if r.complete else "",
                "anti_patterns_hit": r.anti_patterns_hit,
                "attempts_remaining":
                    a.scenario.scoring.max_verify_attempts - a.verify_attempts_used}

    @app.get("/attempts/{attempt_id}/debrief")
    def debrief(attempt_id: str):
        return orch.debrief(_get(attempt_id))

    @app.get("/analytics/summary")
    def analytics_summary():
        return store.summary()

    @app.get("/attempts/{attempt_id}/grades")
    def get_grades(attempt_id: str):
        a = _get(attempt_id)
        existing = store.get_grades(attempt_id)
        if existing:
            return existing
        orch.debrief(a)  # ensure the record is finalized before grading
        out = grader_runner.run(store.events(attempt_id), a.scenario)
        # HARD RULE: anything generated here lands as a proposal. Only the
        # confirm endpoint — a human — writes 'confirmed'.
        store.save_grades(attempt_id, out["grades"], grader=out["grader"],
                          status="proposed")
        return store.get_grades(attempt_id)

    @app.post("/attempts/{attempt_id}/grades/confirm")
    def confirm_grades(attempt_id: str, req: ConfirmGradesReq):
        _get(attempt_id)
        existing = store.get_grades(attempt_id)
        if existing is None:
            raise HTTPException(404, "no proposal to confirm — GET grades first")
        grades = existing["grades"]
        for dim in grades["dimensions"]:
            if dim["name"] in req.overrides:
                dim["score"] = max(0, min(10, req.overrides[dim["name"]]))
                dim["overridden"] = True
        store.save_grades(attempt_id, grades, grader=existing["grader"],
                          status="confirmed", confirmed_by=req.confirmed_by)
        return store.get_grades(attempt_id)

    if demo_mode:
        @app.post("/attempts/{attempt_id}/demo/mutate")
        def demo_mutate(attempt_id: str, req: MutateReq):
            a = _get(attempt_id)
            ad = a.adapter
            if req.action == "set_startup_command" and req.value:
                ad.set_startup_command(req.value)
            elif req.action == "set_limits" and req.memory is not None:
                ad.set_limits(memory=req.memory)
            elif req.action == "start":
                ad.set_power_state("running")
            elif req.action == "stop":
                ad.set_power_state("offline")
            elif req.action == "restart":
                ad.set_power_state("offline")
                ad.set_power_state("running")
            elif req.action == "set_variable" and req.key and req.value is not None:
                ad.set_variable(req.key, req.value)
            elif req.action == "unsuspend":
                ad.unsuspend_server()
            elif req.action == "reinstall":
                ad.reinstall_server()
            else:
                raise HTTPException(422, f"unknown demo action {req.action}")
            snap = ad.snapshot()
            return {"power_state": snap.power_state,
                    "startup_command": snap.startup_command,
                    "limits": snap.limits, "variables": snap.variables}

        @app.post("/attempts/{attempt_id}/demo/advance_clock")
        def demo_advance(attempt_id: str, req: AdvanceClockReq):
            a = _get(attempt_id)
            a.clock.advance(req.seconds)
            return {"now": a.clock.now()}

        @app.get("/attempts/{attempt_id}/demo/panel")
        def demo_panel(attempt_id: str):
            snap = _get(attempt_id).adapter.snapshot()
            return {"power_state": snap.power_state,
                    "startup_command": snap.startup_command,
                    "limits": snap.limits, "variables": snap.variables,
                    "activity": list(snap.activity)[-10:]}

    frontend = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend.exists():
        @app.get("/")
        def index():
            return FileResponse(frontend)

    return app
