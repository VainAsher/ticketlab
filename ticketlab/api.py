"""FastAPI surface. Trainee-facing responses NEVER include fault or solution
content — the response models are explicit allowlists, not filtered dumps.

Demo endpoints simulate trainee panel actions against the MockAdapter so the
whole loop can be presented with no Pterodactyl instance in the room. In
panel mode (Phase 2 of rollout) these endpoints are disabled and the trainee
acts in the real iframed panel instead.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from ticketlab.schema import (load_scenario, load_scenario_from_dict, Scenario,
                              FAULT_VERBS, ASSERTION_TYPES, OPERATORS, GRADE_RANK)
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


class ScenarioDraft(BaseModel):
    scenario: dict


class UpdateCardReq(BaseModel):
    last4: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2000, le=2100)


class RetryPaymentReq(BaseModel):
    invoice_id: str = Field(min_length=1, max_length=100)


def _identity(request: Request) -> str:
    """Trainee identity from the Authentik forward-auth headers Traefik
    injects. Absent (local dev, tests) -> 'anonymous'. Never trusted for
    authorization — attribution only."""
    return (request.headers.get("x-authentik-username")
            or request.headers.get("x-authentik-email")
            or "anonymous")[:100]


# Per-verb / per-assertion field requirements + authoring hints. Served by
# /authoring/vocab so the trainer form is generated from the same source of
# truth as the schema whitelist — the two cannot drift apart silently.
FAULT_VERB_SPEC = {
    "set_startup_command": {"fields": ["value"], "hint": "Replace the server's startup command — the classic 'customer pasted something' fault."},
    "set_variable": {"fields": ["key", "value"], "hint": "Set an egg variable (e.g. SERVER_JARFILE) to a broken value."},
    "set_limits": {"fields": ["memory", "disk", "cpu"], "hint": "Change container limits. Only fill the ones you want to change."},
    "write_file": {"fields": ["path", "content"], "hint": "Plant a file on the server (e.g. a corrupt config)."},
    "delete_file": {"fields": ["path"], "hint": "Remove a file the server needs."},
    "reassign_allocation": {"fields": [], "hint": "Not modelled in the mock adapter yet — avoid for now."},
    "start_server": {"fields": [], "hint": "Power on (crash rules apply — an impossible start ends offline)."},
    "stop_server": {"fields": [], "hint": "Power off cleanly."},
    "kill_server": {"fields": [], "hint": "Hard kill."},
    "suspend_server": {"fields": [], "hint": "Legacy game-panel-only suspend. Prefer the billing verbs below (set_payment_method / create_invoice / fail_payment / suspend_account) so suspension is fixed through the Billing panel, not a bare Unsuspend button."},
    "wait": {"fields": ["seconds"], "hint": "Let simulated time pass between steps."},
    "set_payment_method": {"fields": ["last4", "exp_month", "exp_year", "card_status"], "hint": "Set the customer's card on file. card_status: valid/expired. Only fields you set are changed."},
    "create_invoice": {"fields": ["invoice_id", "amount", "status", "due_date", "note"], "hint": "Add a billing invoice. Give it a stable id — your win-condition assertions will reference it."},
    "fail_payment": {"fields": ["invoice_id"], "hint": "Marks that invoice failed and moves the account to 'overdue' (not suspended yet — good for a grace-period scenario)."},
    "suspend_account": {"fields": ["note"], "hint": "Moves the account to 'suspended' — the game panel's Start is denied until account_status is active again, no matter what's changed there. 'note' becomes the suspension reason shown in the Billing panel."},
}
ASSERTION_SPEC = {
    "server_state": {"fields": ["operator", "expected", "stable_for_seconds"], "hint": "Power state check. Use stable_for_seconds to demand it HOLDS (uptime-anchored) — 60s is the house norm."},
    "startup_command": {"fields": ["operator", "expected"], "hint": "Check the startup command text (contains / not_contains / matches regex)."},
    "variable_equals": {"fields": ["field", "operator", "expected"], "hint": "Check an egg variable. 'field' is the variable name."},
    "limits_check": {"fields": ["field", "operator", "expected"], "hint": "Check a container limit (field: memory/disk/cpu; gte/lte for thresholds)."},
    "file_contains": {"fields": ["field", "expected"], "hint": "File at path 'field' must contain 'expected'."},
    "file_absent": {"fields": ["field"], "hint": "File at path 'field' must not exist."},
    "activity_occurred": {"fields": ["event"], "hint": "Something must have happened, ever (reads the activity log — good for 'they unsuspended it': server:suspension.update)."},
    "activity_absent": {"fields": ["event"], "hint": "Something must NEVER have happened (good for anti-patterns)."},
    "allocation_check": {"fields": [], "hint": "Not modelled in the mock adapter — always passes. Avoid."},
    "invoice_status": {"fields": ["field", "operator", "expected"], "hint": "'field' is the invoice_id from your fault script; 'expected' is pending/paid/failed."},
    "payment_method_valid": {"fields": ["operator", "expected"], "hint": "Checks the card status on file. expected: valid/expired."},
    "account_status": {"fields": ["operator", "expected"], "hint": "expected: active/overdue/suspended."},
}


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
    builtin_ids = frozenset(scenarios)

    # Trainer-authored scenarios live NEXT TO THE DB (a volume in Docker), not
    # in the baked image dir — they must survive a redeploy. Loaded after the
    # builtins; an authored id never shadows a builtin (publish rejects it).
    authored_dir = Path(db_path).parent / "scenarios-authored"
    authored_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(authored_dir.glob("*.yaml")):
        s = load_scenario(p)
        if s.metadata.id not in builtin_ids:
            scenarios[s.metadata.id] = s

    @app.get("/scenarios")
    def list_scenarios():
        return [{"id": s.metadata.id, "title": s.metadata.title,
                 "difficulty": s.metadata.difficulty,
                 "estimated_minutes": s.metadata.estimated_minutes}
                for s in scenarios.values()]

    def _scenario_brief(s: Scenario) -> dict:
        return {  # trainee-safe brief — counts and goals, never content
            "id": s.metadata.id,
            "title": s.metadata.title,
            "difficulty": s.metadata.difficulty,
            "estimated_minutes": s.metadata.estimated_minutes,
            "objectives": s.metadata.objectives or DEFAULT_OBJECTIVES,
            "facts_total": len(s.conversation.hidden_facts),
        }

    @app.post("/attempts", status_code=201)
    def create_attempt(req: CreateAttemptReq, request: Request):
        s = scenarios.get(req.scenario_id)
        if s is None:
            raise HTTPException(404, "unknown scenario")
        a = orch.create_attempt(s, trainee=_identity(request))
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
            "scenario": _scenario_brief(s),
        }

    def _get(attempt_id: str):
        a = orch.get(attempt_id)
        if a is None:
            raise HTTPException(404, "unknown attempt")
        return a

    @app.get("/attempts/{attempt_id}/state")
    def attempt_state(attempt_id: str):
        """Trainee-safe resume snapshot — lets the page reload without losing
        the attempt. Same allowlist discipline as create_attempt: thread text
        only, never fault/solution content."""
        a = _get(attempt_id)
        s = a.scenario
        conv = a.conversation.state
        return {
            "attempt_id": a.id,
            "ticket": {"subject": s.ticket.subject, "priority": s.ticket.priority,
                      "customer_name": s.ticket.customer.name,
                      "body": s.ticket.body},
            "scenario": _scenario_brief(s),
            "satisfaction": conv.satisfaction,
            "satisfaction_start": s.conversation.satisfaction_start,
            "escalated": conv.escalated,
            "verify_budget": s.scoring.max_verify_attempts,
            "verify_attempts_used": a.verify_attempts_used,
            "facts_revealed": sorted(conv.revealed),
            "thread": [{"role": m["role"], "text": m["text"]}
                      for m in conv.thread[1:]],  # [0] is the opening ticket body
        }

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

    # ── trainer surface: monitoring ──
    @app.get("/trainer/scenarios")
    def trainer_scenarios():
        return [{"id": s.metadata.id, "title": s.metadata.title,
                 "difficulty": s.metadata.difficulty, "tags": s.metadata.tags,
                 "estimated_minutes": s.metadata.estimated_minutes,
                 "version": s.metadata.version,
                 "origin": "builtin" if s.metadata.id in builtin_ids else "authored",
                 "facts_total": len(s.conversation.hidden_facts),
                 "solutions": [{"id": sol.id, "grade": sol.grade,
                                "score": sol.score, "label": sol.label}
                               for sol in s.verification.solutions]}
                for s in scenarios.values()]

    @app.get("/trainer/attempts")
    def trainer_attempts(scenario_id: str | None = None,
                         trainee: str | None = None, limit: int = 200):
        return store.list_attempts(scenario_id=scenario_id, trainee=trainee,
                                   limit=limit)

    @app.get("/trainer/attempts/{attempt_id}")
    def trainer_attempt_detail(attempt_id: str):
        rec = store.get_record(attempt_id)
        if rec is None:
            raise HTTPException(404, "unknown attempt")
        return {"record": rec, "events": store.events(attempt_id),
                "grades": store.get_grades(attempt_id)}

    # ── trainer surface: authoring ──
    @app.get("/authoring/vocab")
    def authoring_vocab():
        personas = sorted({s.conversation.persona for s in scenarios.values()}
                          | {s.ticket.customer.persona for s in scenarios.values()})
        return {
            "fault_verbs": {v: FAULT_VERB_SPEC.get(v, {"fields": [], "hint": ""})
                            for v in sorted(FAULT_VERBS)},
            "assertion_types": {t: ASSERTION_SPEC.get(t, {"fields": [], "hint": ""})
                                for t in sorted(ASSERTION_TYPES)},
            "operators": sorted(OPERATORS),
            "grades": list(GRADE_RANK),        # temp < partial < full
            "personas": personas,
            "crash_rules": ["startup_contains", "startup_matches",
                            "variable_equals", "heap_exceeds_limit"],
            "adapters": ["mock"],              # panel adapters arrive post-MVP
            "provenance_sources": ["generic", "internal"],
        }

    def _validate_draft(raw: dict) -> tuple[Scenario | None, list[dict]]:
        try:
            return load_scenario_from_dict(raw), []
        except ValidationError as e:
            return None, [{"loc": ".".join(str(x) for x in err["loc"]),
                           "msg": err["msg"]} for err in e.errors()]

    @app.post("/authoring/validate")
    def authoring_validate(req: ScenarioDraft):
        s, errors = _validate_draft(req.scenario)
        if s is not None and s.metadata.id in builtin_ids:
            errors.append({"loc": "metadata.id",
                           "msg": "id collides with a built-in scenario"})
        return {"valid": not errors, "errors": errors}

    @app.post("/authoring/scenarios", status_code=201)
    def authoring_publish(req: ScenarioDraft):
        s, errors = _validate_draft(req.scenario)
        if s is None:
            raise HTTPException(422, detail=errors)
        if s.metadata.id in builtin_ids:
            raise HTTPException(409, "id collides with a built-in scenario")
        path = authored_dir / f"{s.metadata.id}.yaml"
        replaced = path.exists()
        path.write_text(yaml.safe_dump(req.scenario, sort_keys=False,
                                       allow_unicode=True), encoding="utf-8")
        scenarios[s.metadata.id] = s   # live immediately, no restart
        return {"id": s.metadata.id, "replaced": replaced,
                "path": path.name}

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

        # ── billing panel demo ── separate from the game panel above:
        # updating a card or retrying a payment never touches server config,
        # and a suspended account blocks Start regardless of what the game
        # panel shows (see MockAdapter.billing_gate).
        @app.get("/attempts/{attempt_id}/demo/billing_panel")
        def demo_billing_panel(attempt_id: str):
            bsnap = _get(attempt_id).billing.snapshot()
            return {"account_status": bsnap.account_status,
                    "suspension_reason": bsnap.suspension_reason,
                    "payment_method": bsnap.payment_method,
                    "invoices": list(bsnap.invoices)}

        @app.post("/attempts/{attempt_id}/demo/billing/update_card")
        def demo_update_card(attempt_id: str, req: UpdateCardReq):
            a = _get(attempt_id)
            a.billing.update_payment_method(req.last4, req.exp_month, req.exp_year)
            bsnap = a.billing.snapshot()
            return {"account_status": bsnap.account_status,
                    "payment_method": bsnap.payment_method}

        @app.post("/attempts/{attempt_id}/demo/billing/retry_payment")
        def demo_retry_payment(attempt_id: str, req: RetryPaymentReq):
            a = _get(attempt_id)
            ok = a.billing.retry_payment(req.invoice_id)
            bsnap = a.billing.snapshot()
            return {"success": ok, "account_status": bsnap.account_status,
                    "invoices": list(bsnap.invoices)}

    frontend = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend.exists():
        @app.get("/")
        def index():
            return FileResponse(frontend)

    trainer_page = Path(__file__).parent.parent / "frontend" / "trainer.html"
    if trainer_page.exists():
        @app.get("/trainer")
        def trainer():
            return FileResponse(trainer_page)

    return app
