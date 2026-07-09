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
    # D12 — populated only on FAILED checks:
    # hint: what this scenario's hint_level allows the trainee to see now.
    # unmet/closest_feedback: full-reveal material, consumed at DEBRIEF only
    # (the API never puts these in a mid-attempt verify response).
    hint: str = ""
    unmet: list[dict] = field(default_factory=list)
    closest_feedback: str = ""


# Fixed engine strings for hint_level=nudge, keyed on the unmet assertion's
# TYPE (D12 ruling 1). Fixed = non-spoiler by construction: nothing
# scenario-specific goes in, so nothing scenario-specific can leak.
NUDGE_STABILITY = ("Your fix may need time to prove itself — wait a little "
                   "before re-verifying.")
NUDGE_BY_TYPE = {
    "server_state": "The server isn't in the state the customer needs yet.",
    "invoice_status": "The underlying account or billing issue isn't cleared yet.",
    "account_status": "The underlying account or billing issue isn't cleared yet.",
    "payment_method_valid": "The underlying account or billing issue isn't cleared yet.",
    "activity_occurred": "Something the fix requires hasn't been done yet.",
}
NUDGE_DEFAULT = "The server's configuration isn't what the fix needs yet."

_OP_PHRASE = {
    "equals": "is", "not_equals": "is not", "contains": "contains",
    "not_contains": "does not contain", "matches": "matches",
    "gte": "is at least", "lte": "is at most",
}


def describe_assertion(a: Assertion) -> str:
    """Human-readable requirement clause. Used for hint_level=explicit and the
    debrief's full reveal — never sent mid-attempt at none/nudge."""
    op = _OP_PHRASE.get(a.operator or "")
    if a.type == "server_state":
        base = f"server power state {op or 'is'} '{a.expected}'"
        if a.stable_for_seconds > 0:
            base += f" and stays that way for {a.stable_for_seconds}s"
        return base
    if a.type == "startup_command":
        return f"startup command {op or 'contains'} '{a.expected}'"
    if a.type == "variable_equals":
        return f"variable '{a.field}' {op or 'is'} '{a.expected}'"
    if a.type == "limits_check":
        return f"{a.field or 'memory'} limit {op or 'is at least'} {a.expected}"
    if a.type == "file_contains":
        return f"file '{a.field}' contains '{a.expected}'"
    if a.type == "file_absent":
        return f"file '{a.field or a.expected}' is absent"
    if a.type == "activity_occurred":
        return f"'{a.event or a.expected}' has been done"
    if a.type == "activity_absent":
        return f"'{a.event or a.expected}' was never done"
    if a.type == "invoice_status":
        return f"invoice '{a.field}' {op or 'is'} '{a.expected}'"
    if a.type == "payment_method_valid":
        return f"payment method {op or 'is'} '{a.expected or 'valid'}'"
    if a.type == "account_status":
        return f"account status {op or 'is'} '{a.expected}'"
    return a.id or a.type


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


def _assertion_holds_now(a: Assertion, snap: PanelSnapshot, bsnap=None) -> bool:
    """Point-in-time evaluation, ignoring stability windows."""
    if a.type == "invoice_status":
        inv = next((i for i in (bsnap.invoices if bsnap else ())
                   if i["id"] == a.field), None)
        return inv is not None and _compare(inv["status"], a.operator or "equals", a.expected)
    if a.type == "payment_method_valid":
        status = bsnap.payment_method["status"] if bsnap else ""
        return _compare(status, a.operator or "equals", a.expected or "valid")
    if a.type == "account_status":
        return _compare(bsnap.account_status if bsnap else "", a.operator or "equals", a.expected)
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
    def __init__(self, verification: Verification, adapter, clock, billing=None):
        self.cfg = verification
        self.adapter = adapter
        self.billing = billing
        self.clock = clock
        # assertion key -> timestamp when it FIRST held continuously
        self._held_since: dict[str, float] = {}

    def _akey(self, sol_id: str, a: Assertion, idx: int) -> str:
        return f"{sol_id}/{a.id or idx}"

    def _assertion_satisfied(self, key: str, a: Assertion, snap: PanelSnapshot,
                             bsnap) -> tuple[bool, bool, float]:
        """-> (satisfied, holds_now, held_seconds). holds_now/held feed the
        D12 hints: holding-but-window-pending is the 'wait' case."""
        holds = _assertion_holds_now(a, snap, bsnap)
        now = self.clock.now()
        if not holds:
            self._held_since.pop(key, None)   # window resets on any lapse
            return False, False, 0.0
        if a.stable_for_seconds <= 0:
            return True, True, 0.0
        if a.type == "server_state":
            # Anchor to the ADAPTER's power transition time (uptime), not our
            # first observation — a trainee shouldn't burn a verify attempt
            # just to start the stability clock. Real Pterodactyl exposes
            # uptime via the resources endpoint, so this survives the swap.
            held = now - snap.power_since
        else:
            held = now - self._held_since.setdefault(key, now)
        return held >= a.stable_for_seconds, True, held

    def _hint(self, first_unmet: Assertion | None, holds_now: bool,
              held: float) -> str:
        level = getattr(self.cfg, "hint_level", "nudge")
        if level == "none" or first_unmet is None:
            return ""
        if level == "explicit":
            desc = describe_assertion(first_unmet)
            if holds_now and first_unmet.stable_for_seconds > 0:
                return (f"Almost: {desc} — held {int(held)}s of "
                        f"{first_unmet.stable_for_seconds}s so far.")
            return f"Unmet: {desc}."
        # nudge — fixed engine strings only. Holding-but-window-pending gets
        # the wait nudge whatever the type: 'wait' is the truthful advice.
        if holds_now and first_unmet.stable_for_seconds > 0:
            return NUDGE_STABILITY
        return NUDGE_BY_TYPE.get(first_unmet.type, NUDGE_DEFAULT)

    def check(self) -> VerifyResult:
        snap = self.adapter.snapshot()
        bsnap = self.billing.snapshot() if self.billing else None
        detail: dict[str, bool] = {}

        matched = None
        # per solution: (satisfied count, unmet assertions with hold state)
        progress: list[tuple] = []
        for sol in self.cfg.solutions:
            ok, sat_count, unmet = True, 0, []
            for i, a in enumerate(sol.assertions):
                key = self._akey(sol.id, a, i)
                sat, holds_now, held = self._assertion_satisfied(key, a, snap, bsnap)
                detail[key] = sat
                ok = ok and sat
                if sat:
                    sat_count += 1
                else:
                    unmet.append((a, holds_now, held))
            progress.append((sol, sat_count, unmet))
            if ok and (matched is None or GRADE_RANK[sol.grade] > GRADE_RANK[matched.grade]):
                matched = sol

        hits, hit_feedback, penalty = [], [], 0
        for ap in self.cfg.anti_patterns:
            if all(_assertion_holds_now(a, snap, bsnap) for a in ap.assertions):
                hits.append(ap.id)
                hit_feedback.append(ap.feedback)
                penalty += ap.penalty

        if matched is None:
            # Closest solution = most assertions satisfied, ties to the
            # higher grade (the fix we'd rather teach). Its first unmet
            # assertion keys the hint; the full unmet map feeds the debrief.
            closest, _, closest_unmet = max(
                progress, key=lambda p: (p[1], GRADE_RANK[p[0].grade]))
            first_unmet, holds_now, held = (closest_unmet[0] if closest_unmet
                                            else (None, False, 0.0))
            return VerifyResult(
                complete=False, anti_patterns_hit=hits,
                anti_pattern_feedback=hit_feedback,
                assertion_detail=detail,
                hint=self._hint(first_unmet, holds_now, held),
                unmet=[{"solution": sol.label, "grade": sol.grade,
                        "unmet": [describe_assertion(a) for a, _, _ in unmet]}
                       for sol, _, unmet in progress],
                closest_feedback=closest.feedback,
            )
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
