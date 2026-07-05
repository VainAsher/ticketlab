"""MockAdapter — fixture-first: this is both the CI fixture and the no-panel
demo backend. State is mutable via the same verbs the fault engine uses and
via demo methods the frontend exposes (simulating what a trainee would do in
a real panel).
"""
from __future__ import annotations

from ticketlab.adapters.base import PanelSnapshot


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class MockAdapter:
    def __init__(self, clock=None):
        self.clock = clock or FakeClock()
        self.power_state = "offline"
        self.power_since = self.clock.now()
        self.startup_command = ""
        self.variables: dict[str, str] = {}
        self.limits: dict[str, int] = {}
        self.files: dict[str, str] = {}
        self.activity: list[str] = []

    # ── lifecycle ──
    def provision(self, server_spec, physics=None) -> None:
        self._rules = list(physics or [])
        self.variables = dict(server_spec.variables)
        self.limits = dict(server_spec.limits)
        self.startup_command = "java -Xms128M -Xmx1024M -jar server.jar"
        self._power("offline")
        self._log("server:provisioned")

    def apply_fault(self, steps) -> None:
        for step in steps:
            action = step.action
            if action == "set_startup_command":
                self.startup_command = step.value
            elif action == "set_variable":
                self.variables[step.key] = step.value
            elif action == "set_limits":
                for f in ("memory", "disk", "cpu"):
                    v = getattr(step, f, None)
                    if v is not None:
                        self.limits[f] = v
            elif action == "write_file":
                self.files[step.path] = step.content or ""
            elif action == "delete_file":
                self.files.pop(step.path, None)
            elif action == "start_server":
                self._power(self._physics("running"))
            elif action in ("stop_server", "kill_server"):
                self._power("offline")
            elif action == "suspend_server":
                self._power("suspended")
            elif action == "wait":
                self.clock.advance(step.seconds or 0) if isinstance(self.clock, FakeClock) else None
            # reassign_allocation: not modelled in MVP mock

    # ── trainee/demo mutations (what fixing looks like) ──
    def set_startup_command(self, value: str) -> None:
        self.startup_command = value
        self._log("server:startup.update")

    def set_variable(self, key: str, value: str) -> None:
        self.variables[key] = value
        self._log("server:startup.update")

    def set_limits(self, **kwargs) -> None:
        for f, v in kwargs.items():
            if v is not None:
                self.limits[f] = v
        self._log("server:build.update")

    def set_power_state(self, state: str) -> None:
        effective = self._physics(state)
        self._power(effective)
        self._log(f"server:power.{effective}")

    def _physics(self, requested: str) -> str:
        """Scenario-declared crash rules + built-in heap rule. A suspended
        server refuses power-on until unsuspended (matches real panels)."""
        import re
        if requested != "running":
            return requested
        if self.power_state == "suspended":
            self._log("server:power.denied-suspended")
            return "suspended"
        # built-in: heap larger than container limit -> OOM kill
        m = re.search(r"-Xmx(\d+)M", self.startup_command)
        limit = self.limits.get("memory", 0)
        if m and limit and int(m.group(1)) > limit:
            self._log("server:power.crash")
            return "offline"
        for r in getattr(self, "_rules", []):
            crashed = (
                (r.when == "startup_contains" and r.value in self.startup_command)
                or (r.when == "startup_matches"
                    and re.search(r.value, self.startup_command))
                or (r.when == "variable_equals"
                    and self.variables.get(r.key or "") == r.value)
                or (r.when == "heap_exceeds_limit" and False)  # built-in above
            )
            if crashed:
                self._log("server:power.crash")
                return "offline"
        return requested

    def unsuspend_server(self) -> None:
        if self.power_state == "suspended":
            self._power("offline")
        self._log("server:suspension.update")

    def reinstall_server(self) -> None:
        self.files.clear()
        self._power("offline")
        self._log("server:settings.reinstall")

    # ── read ──
    def snapshot(self) -> PanelSnapshot:
        return PanelSnapshot(
            power_state=self.power_state,
            startup_command=self.startup_command,
            variables=dict(self.variables),
            limits=dict(self.limits),
            files=dict(self.files),
            activity=tuple(self.activity),
            power_since=self.power_since,
            taken_at=self.clock.now(),
        )

    def _power(self, state: str) -> None:
        if state != self.power_state:
            self.power_since = self.clock.now()
        self.power_state = state

    def _log(self, event: str) -> None:
        self.activity.append(event)
