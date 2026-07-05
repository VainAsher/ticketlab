"""LLM client protocol. The engine calls customer_turn(ctx) and treats the
result as a PROPOSAL — reveals are filtered, deltas clamped, by the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnContext:
    persona: str
    customer_name: str
    ticket_subject: str
    ticket_body: str
    thread: list[dict]                 # [{role: trainee|customer, text}]
    trainee_message: str
    state_events: list[str]            # customer-observable, pre-filtered
    earned_facts: list[str]            # fact TEXTS the trainee just earned
    satisfaction: int


@dataclass
class LLMTurn:
    reply: str
    facts_revealed: list[str] = field(default_factory=list)   # fact IDs
    satisfaction_delta: int = 0
