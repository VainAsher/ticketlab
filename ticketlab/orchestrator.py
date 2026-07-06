"""Orchestrator — owns attempt lifecycle and joins the three consumers of
panel state: verifier (everything, decides completion), conversation
(customer-observable only), debrief (everything, after the fact).

Pending observable events accumulate between customer turns: a fix made while
the customer is 'away' is noticed on their next reply, like real tickets.
Storage is in-memory for MVP (single-process internal tool); the store is a
class so a SQLite/Postgres implementation can replace it without touching
callers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ticketlab.schema import Scenario
from ticketlab.adapters.mock import MockAdapter, FakeClock
from ticketlab.verifier import Verifier, VerifyResult
from ticketlab.conversation import ConversationEngine, TurnResult
from ticketlab.statefilter import customer_observable_events


@dataclass
class Attempt:
    id: str
    scenario: Scenario
    adapter: MockAdapter
    clock: FakeClock
    verifier: Verifier
    conversation: ConversationEngine
    verify_attempts_used: int = 0
    last_verify: VerifyResult | None = None
    best_verify: VerifyResult | None = None
    _last_seen_snapshot: object = None
    pending_events: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, llm=None, max_attempts: int = 200, store=None):
        self._attempts: dict[str, Attempt] = {}   # insertion-ordered (LRU-ish)
        self._llm = llm
        self._max_attempts = max_attempts
        self._store = store   # AttemptStore or None; records outlive eviction

    def create_attempt(self, scenario: Scenario,
                       trainee: str = "anonymous") -> Attempt:
        if scenario.panel.adapter != "mock":
            # A lying test environment is worse than a missing one: never run
            # a panel-targeted scenario against the mock silently. (REVIEW A4)
            raise NotImplementedError(
                f"adapter '{scenario.panel.adapter}' not available in this "
                "build; MVP supports 'mock' only")
        while len(self._attempts) >= self._max_attempts:   # REVIEW B3
            self._attempts.pop(next(iter(self._attempts)))
        clock = FakeClock()
        adapter = MockAdapter(clock=clock)
        adapter.provision(scenario.environment.server,
                          physics=scenario.environment.mock_physics)
        adapter.apply_fault(scenario.fault.steps)
        attempt = Attempt(
            id=uuid.uuid4().hex[:12],
            scenario=scenario,
            adapter=adapter,
            clock=clock,
            verifier=Verifier(scenario.verification, adapter, clock),
            conversation=ConversationEngine(scenario, llm=self._llm),
        )
        attempt._last_seen_snapshot = adapter.snapshot()
        self._attempts[attempt.id] = attempt
        if self._store:
            self._store.start_attempt(attempt.id, scenario.metadata.id,
                                      trainee=trainee)
        return attempt

    def get(self, attempt_id: str) -> Attempt | None:
        return self._attempts.get(attempt_id)

    # ── conversation plane ──
    def trainee_message(self, attempt: Attempt, text: str) -> TurnResult:
        current = attempt.adapter.snapshot()
        new_events = customer_observable_events(attempt._last_seen_snapshot, current)
        attempt.pending_events.extend(new_events)
        events, attempt.pending_events = attempt.pending_events, []
        attempt._last_seen_snapshot = current
        turn = attempt.conversation.trainee_message(text, state_events=events)
        if self._store:
            self._store.log_event(attempt.id, "message", {
                "text": text, "reply": turn.reply,
                "facts_revealed": turn.facts_revealed,
                "satisfaction": turn.satisfaction,
                "state_events": events,
                "escalated": turn.escalated,
            }, ts=attempt.clock.now())
        return turn

    # ── technical plane ──
    def verify(self, attempt: Attempt) -> VerifyResult | None:
        if attempt.verify_attempts_used >= attempt.scenario.scoring.max_verify_attempts:
            return None  # budget exhausted; API maps to 429
        attempt.verify_attempts_used += 1
        result = attempt.verifier.check()
        attempt.last_verify = result
        if result.complete and (attempt.best_verify is None
                                or result.score > attempt.best_verify.score):
            attempt.best_verify = result
        if self._store:
            self._store.log_event(attempt.id, "verify", {
                "complete": result.complete, "grade": result.grade,
                "score": result.score,
                "anti_patterns": result.anti_patterns_hit,
            }, ts=attempt.clock.now())
        return result

    # ── debrief: both planes, full visibility ──
    def debrief(self, attempt: Attempt) -> dict:
        result = self._debrief_dict(attempt)
        if self._store:
            self._store.finalize(attempt.id, result)
        return result

    def _debrief_dict(self, attempt: Attempt) -> dict:
        conv = attempt.conversation.state
        best = attempt.best_verify
        return {
            "technical": {
                "complete": bool(best and best.complete),
                "grade": best.grade if best else None,
                "score": best.score if best else 0,
                "solution": best.matched_solution if best else None,
                "feedback": best.feedback if best else
                            "No solution verified before the attempt ended.",
                "anti_patterns": best.anti_patterns_hit if best else [],
                "anti_pattern_feedback": best.anti_pattern_feedback if best else [],
            },
            "conversation": {
                "satisfaction_final": conv.satisfaction,
                "escalated": conv.escalated,
                "facts_uncovered": len(conv.revealed),
                "facts_total": len(attempt.scenario.conversation.hidden_facts),
                "facts_missed": [
                    f.id for f in attempt.scenario.conversation.hidden_facts
                    if f.id not in conv.revealed
                ],
                "turns": conv.turns,
            },
            "verify_attempts_used": attempt.verify_attempts_used,
        }
