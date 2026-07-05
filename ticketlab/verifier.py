"""Verifier — deterministic state machine over PanelSnapshots.

Completion is decided HERE, never by an LLM. Highest-grade matching solution
wins; anti-patterns are historical (read from the activity log) and penalise
the matched score. `stable_for_seconds` is tracked per-assertion: the window
resets whenever the assertion stops holding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ticketlab.schema import Assertion, Verification, GRADE_RANK
from ticketlab.adapters.base import PanelSnapshot


@dataclass
class VerifyResult:
    complete: bool
    matched_solution: str | None = None
    grade: str | None = None
    score: int = 0
    feedback: str = ""
    anti_patterns_hit: list[str] = field(default_factory=list)
    anti_pattern_feedback: list[str] = field(default_factory=list)
    assertion_detail: dict[str, bool] = field(default_factory=dict)


def _compare(actual, operator: str, expected) -> bool:
    if operator == "equals":
        return str(actual) == str(expected)
    if operator == "not_equals":
        return str(actual) != str(expected)
    if operator == "contains":
        return str(expected) in str(actual)
    if operator == "not_contains":
        return str(expected) not in str(actual)
    if operator == "matches":
        return re.search(str(expected), str(actual)) is not None
    if operator == "gte":
        return int(actual) >= int(expected)
    if operator == "lte":
        return int(actual) <= int(expected)
    raise ValueError(f"unknown operator {operator}")


def _assertion_holds_now(a: Assertion, snap: PanelSnapshot) -> bool:
    """Point-in-time evaluation, ignoring stability windows."""
    if a.type == "server_state":
        return _compare(snap.power_state, a.operator or "equals", a.expected)
    if a.type == "startup_command":
        return _compare(snap.startup_command, a.operator or "contains", a.expected)
    if a.type == "variable_equals":
        return _compare(snap.variables.get(a.field or "", ""),
                        a.operator or "equals", a.expected)
    if a.type == "limits_check":
        return _compare(snap.limits.get(a.field or "memory", 0), a.operator or "gte", a.expected)
    if a.type == "file_contains":
        return _compare(snap.files.get(a.field or "", ""), "contains", a.expected)
    if a.type == "file_absent":
        return (a.field or str(a.expected)) not in snap.files
    if a.type == "activity_occurred":
        return (a.event or str(a.expected)) in snap.activity
    if a.type == "activity_absent":
        return (a.event or str(a.expected)) not in snap.activity
    if a.type == "allocation_check":
        return True  # not modelled in MVP mock
    raise ValueError(f"unknown assertion type {a.type}")


class Verifier:
    def __init__(self, verification: Verification, adapter, clock):
        self.cfg = verification
        self.adapter = adapter
        self.clock = clock
        # assertion key -> timestamp when it FIRST held continuously
        self._held_since: dict[str, float] = {}

    def _akey(self, sol_id: str, a: Assertion, idx: int) -> str:
        return f"{sol_id}/{a.id or idx}"

    def _assertion_satisfied(self, key: str, a: Assertion, snap: PanelSnapshot) -> bool:
        holds = _assertion_holds_now(a, snap)
        now = self.clock.now()
        if not holds:
            self._held_since.pop(key, None)   # window resets on any lapse
            return False
        if a.stable_for_seconds <= 0:
            return True
        if a.type == "server_state":
            # Anchor to the ADAPTER's power transition time (uptime), not our
            # first observation — a trainee shouldn't burn a verify attempt
            # just to start the stability clock. Real Pterodactyl exposes
            # uptime via the resources endpoint, so this survives the swap.
            return (now - snap.power_since) >= a.stable_for_seconds
        since = self._held_since.setdefault(key, now)
        return (now - since) >= a.stable_for_seconds

    def check(self) -> VerifyResult:
        snap = self.adapter.snapshot()
        detail: dict[str, bool] = {}

        matched = None
        for sol in self.cfg.solutions:
            ok = True
            for i, a in enumerate(sol.assertions):
                key = self._akey(sol.id, a, i)
                sat = self._assertion_satisfied(key, a, snap)
                detail[key] = sat
                ok = ok and sat
            if ok and (matched is None or GRADE_RANK[sol.grade] > GRADE_RANK[matched.grade]):
                matched = sol

        hits, hit_feedback, penalty = [], [], 0
        for ap in self.cfg.anti_patterns:
            if all(_assertion_holds_now(a, snap) for a in ap.assertions):
                hits.append(ap.id)
                hit_feedback.append(ap.feedback)
                penalty += ap.penalty

        if matched is None:
            return VerifyResult(complete=False, anti_patterns_hit=hits,
                                anti_pattern_feedback=hit_feedback,
                                assertion_detail=detail)
        return VerifyResult(
            complete=True,
            matched_solution=matched.id,
            grade=matched.grade,
            score=max(0, matched.score - penalty),
            feedback=matched.feedback,
            anti_patterns_hit=hits,
            anti_pattern_feedback=hit_feedback,
            assertion_detail=detail,
        )
