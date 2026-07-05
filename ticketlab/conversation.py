"""ConversationEngine — owns ALL conversational state (R4).

The LLM renders prose; this engine decides which hidden facts are earned
(keyword rules), clamps satisfaction deltas, tracks the escalation floor, and
filters any reveals a misbehaving LLM proposes. Falls back to ScriptedLLM on
any LLM failure so the lab never dies mid-attempt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ticketlab.schema import Scenario
from ticketlab.llm.base import TurnContext, LLMTurn
from ticketlab.llm.scripted import ScriptedLLM

MAX_DELTA_PER_TURN = 25
MAX_TRAINEE_MESSAGE_CHARS = 4000


@dataclass
class ConversationState:
    satisfaction: int
    revealed: set[str] = field(default_factory=set)
    escalated: bool = False
    thread: list[dict] = field(default_factory=list)   # [{role, text}]
    turns: int = 0


@dataclass
class TurnResult:
    reply: str
    facts_revealed: list[str]
    satisfaction: int
    satisfaction_delta: int
    escalated: bool


class ConversationEngine:
    def __init__(self, scenario: Scenario, llm=None):
        self.scenario = scenario
        self.llm = llm or ScriptedLLM()
        self._fallback = ScriptedLLM()
        self.state = ConversationState(
            satisfaction=scenario.conversation.satisfaction_start)
        # ticket body is the customer's opening message
        self.state.thread.append({"role": "customer",
                                  "text": scenario.ticket.body.strip()})

    # ── reveal rules: orchestrator-owned, keyword-based for MVP ──
    def _earned_facts(self, message: str) -> list[str]:
        msg = message.lower()
        earned = []
        for f in self.scenario.conversation.hidden_facts:
            if f.id in self.state.revealed:
                continue
            # Stem match: keyword 'chang' hits change/changed/changes.
            # Author keywords as stems in scenario files. (REVIEW C2)
            if any(re.search(rf"\b{re.escape(k.lower())}", msg)
                   for k in f.reveal_keywords):
                earned.append(f.id)
        return earned

    def trainee_message(self, message: str, state_events: list[str]) -> TurnResult:
        message = message[:MAX_TRAINEE_MESSAGE_CHARS]
        if self.state.escalated:
            return TurnResult(
                reply="(This ticket has been escalated — the customer is no "
                      "longer responding to you.)",
                facts_revealed=[], satisfaction=self.state.satisfaction,
                satisfaction_delta=0, escalated=True)

        earned_ids = self._earned_facts(message)
        facts_by_id = {f.id: f for f in self.scenario.conversation.hidden_facts}
        earned_texts = [facts_by_id[i].fact for i in earned_ids]

        ctx = TurnContext(
            persona=self.scenario.conversation.persona,
            customer_name=self.scenario.ticket.customer.name,
            ticket_subject=self.scenario.ticket.subject,
            ticket_body=self.scenario.ticket.body,
            thread=list(self.state.thread),
            trainee_message=message,
            state_events=list(state_events),
            earned_facts=earned_texts,
            satisfaction=self.state.satisfaction,
        )

        try:
            turn: LLMTurn = self.llm.customer_turn(ctx)
        except Exception:  # noqa: BLE001 — any LLM failure -> scripted fallback
            turn = self._fallback.customer_turn(ctx)

        # Filter: LLM may only "reveal" facts the engine says were earned.
        allowed_reveals = [i for i in turn.facts_revealed if i in earned_ids]
        final_reveals = sorted(set(allowed_reveals) | set(earned_ids))

        delta = max(-MAX_DELTA_PER_TURN, min(MAX_DELTA_PER_TURN,
                                             turn.satisfaction_delta))
        self.state.satisfaction = max(0, min(100, self.state.satisfaction + delta))
        self.state.revealed.update(final_reveals)
        if self.state.satisfaction <= 0:
            self.state.escalated = True

        self.state.thread.append({"role": "trainee", "text": message})
        self.state.thread.append({"role": "customer", "text": turn.reply})
        self.state.turns += 1

        return TurnResult(reply=turn.reply, facts_revealed=final_reveals,
                          satisfaction=self.state.satisfaction,
                          satisfaction_delta=delta,
                          escalated=self.state.escalated)
