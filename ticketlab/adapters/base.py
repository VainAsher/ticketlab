"""PanelAdapter protocol + PanelSnapshot.

The anti-corruption layer: verifier, state filter, and orchestrator consume
ONLY this interface. Pterodactyl/Pelican implementations live behind it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class RealClock:
    def now(self) -> float:
        return time.time()


@dataclass(frozen=True)
class PanelSnapshot:
    """Point-in-time state of the training server, panel-agnostic."""
    power_state: str                      # running | offline | starting | stopping
    startup_command: str
    variables: dict[str, str]
    limits: dict[str, int]                # memory/disk/cpu
    files: dict[str, str] = field(default_factory=dict)
    activity: tuple[str, ...] = ()        # ordered event names, oldest first
    power_since: float = 0.0              # when power_state last changed (uptime anchor)
    taken_at: float = 0.0


class PanelAdapter(Protocol):
    def snapshot(self) -> PanelSnapshot: ...
    def provision(self, server_spec) -> None: ...
    def apply_fault(self, steps) -> None: ...
