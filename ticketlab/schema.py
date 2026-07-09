"""Scenario schema (v2) — Pydantic models, verb whitelist, provenance firewall.

Nothing here talks to a panel. Scenario files speak adapter verbs only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# ── Whitelists (Rule of Three: no new entries without three consuming scenarios) ──
FAULT_VERBS = {
    "set_variable", "set_startup_command", "set_limits", "write_file",
    "delete_file", "reassign_allocation", "start_server", "stop_server",
    "kill_server", "suspend_server", "wait",
    # billing panel (ticketlab/adapters/billing.py) — 3 scenarios consume
    # these: suspended-not-broken, billing-overdue-grace-period,
    # billing-payment-fixed-needs-retry
    "set_payment_method", "create_invoice", "fail_payment", "suspend_account",
}
ASSERTION_TYPES = {
    "server_state", "startup_command", "variable_equals", "limits_check",
    "file_contains", "file_absent", "allocation_check", "activity_occurred",
    "activity_absent",
    "invoice_status", "payment_method_valid", "account_status",
}
OPERATORS = {"equals", "not_equals", "contains", "not_contains", "matches", "gte", "lte"}
GRADE_RANK = {"temp": 1, "partial": 2, "full": 3}


class ProvenanceError(Exception):
    """Raised when a scenario's provenance forbids loading in this mode."""


class Provenance(BaseModel):
    created: str
    source: str  # internal | generic | client:<slug>
    licence: str = "proprietary"

    @field_validator("created", mode="before")
    @classmethod
    def _date_to_str(cls, v):  # YAML parses bare dates into datetime.date
        return str(v)

    @field_validator("source")
    @classmethod
    def _source_shape(cls, v: str) -> str:
        if v in ("internal", "generic") or v.startswith("client:"):
            return v
        raise ValueError(f"invalid provenance source: {v}")


class Metadata(BaseModel):
    id: str
    title: str
    version: str
    author: str
    provenance: Provenance
    difficulty: int = Field(ge=1, le=5)
    tags: list[str] = []
    estimated_minutes: int = 15
    # Trainee-VISIBLE goals shown in the UI before the fix is found. Authoring
    # rule: must not name the fault, the solution, or any hidden fact — write
    # what success looks like, not how to get there. Empty list = the API
    # serves generic derived objectives instead.
    objectives: list[str] = []


class PanelCfg(BaseModel):
    adapter: Literal["pterodactyl", "pelican", "mock"]
    min_version: str = "1.11"


class Customer(BaseModel):
    name: str
    persona: str
    context: str = ""


class Ticket(BaseModel):
    subject: str
    priority: str = "medium"
    customer: Customer
    body: str


class ServerSpec(BaseModel):
    name: str
    egg: str
    limits: dict[str, int] = {}
    variables: dict[str, str] = {}


class CrashRule(BaseModel):
    """Declarative mock physics: conditions under which the training server
    cannot hold 'running'. Consumed ONLY by MockAdapter; panel adapters
    ignore this block (real servers have real physics)."""
    when: Literal["startup_contains", "startup_matches", "variable_equals",
                  "heap_exceeds_limit"]
    value: Optional[str] = None
    key: Optional[str] = None


class Environment(BaseModel):
    server: ServerSpec
    mock_physics: list[CrashRule] = []


class FaultStep(BaseModel):
    action: str
    value: Optional[str] = None
    key: Optional[str] = None
    seconds: Optional[int] = None
    memory: Optional[int] = None
    disk: Optional[int] = None
    cpu: Optional[int] = None
    path: Optional[str] = None
    content: Optional[str] = None
    # billing verbs
    last4: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    card_status: Optional[str] = None
    invoice_id: Optional[str] = None
    amount: Optional[int] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    note: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _known_verb(cls, v: str) -> str:
        if v not in FAULT_VERBS:
            raise ValueError(f"unknown fault verb: {v}")
        return v


class Fault(BaseModel):
    steps: list[FaultStep]


class HiddenFact(BaseModel):
    id: str
    fact: str
    reveal_keywords: list[str] = Field(min_length=1)


class Conversation(BaseModel):
    persona: str
    satisfaction_start: int = Field(default=50, ge=0, le=100)
    never_volunteers: bool = True
    hidden_facts: list[HiddenFact] = []


class Assertion(BaseModel):
    id: Optional[str] = None
    type: str
    operator: Optional[str] = None
    expected: Optional[str | int] = None
    field: Optional[str] = None
    event: Optional[str] = None
    stable_for_seconds: int = 0

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in ASSERTION_TYPES:
            raise ValueError(f"unknown assertion type: {v}")
        return v

    @field_validator("operator")
    @classmethod
    def _known_op(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in OPERATORS:
            raise ValueError(f"unknown operator: {v}")
        return v


class Solution(BaseModel):
    id: str
    grade: Literal["full", "partial", "temp"]
    score: int = Field(ge=0, le=100)
    label: str
    assertions: list[Assertion] = Field(min_length=1)
    feedback: str = ""


class AntiPattern(BaseModel):
    id: str
    penalty: int = Field(ge=0, le=100)
    assertions: list[Assertion] = Field(min_length=1)
    feedback: str = ""


class Verification(BaseModel):
    poll_interval_seconds: int = 15
    solutions: list[Solution] = Field(min_length=1)
    anti_patterns: list[AntiPattern] = []


class Scoring(BaseModel):
    max_verify_attempts: int = 5
    target_minutes: int = 15


class Teardown(BaseModel):
    policy: str = "on_pass_or_expiry"
    expiry_minutes: int = 90


class Scenario(BaseModel):
    schema_version: int
    metadata: Metadata
    panel: PanelCfg
    ticket: Ticket
    environment: Environment
    fault: Fault
    conversation: Conversation
    verification: Verification
    scoring: Scoring = Scoring()
    teardown: Teardown = Teardown()


def load_scenario_from_dict(raw: dict, product_mode: bool = False) -> Scenario:
    scenario = Scenario.model_validate(raw)
    if product_mode and scenario.metadata.provenance.source == "internal":
        raise ProvenanceError(
            f"scenario '{scenario.metadata.id}' is provenance=internal; "
            "internal content never ships in product mode"
        )
    return scenario


def load_scenario(path: str | Path, product_mode: bool = False) -> Scenario:
    # explicit utf-8: scenario yamls are utf-8; locale-default decoding
    # mangles non-ascii titles on Windows (TL-1)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return load_scenario_from_dict(raw, product_mode=product_mode)
