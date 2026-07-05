"""State filter — translates PanelSnapshot diffs into what a CUSTOMER can see.

The honesty layer of the simulation: the customer agent never learns about
startup commands, variables, or limits directly — only observable consequences.
Panel internals stay invisible; power transitions and destructive events become
customer-visible experiences.
"""
from __future__ import annotations

from ticketlab.adapters.base import PanelSnapshot

_POWER_PHRASES = {
    ("offline", "running"): "The customer's server has just come online.",
    ("running", "offline"): "The customer's server has just gone offline.",
    ("running", "suspended"): "The customer's server was suspended.",
    ("offline", "suspended"): "The customer's server was suspended.",
    ("suspended", "running"): "The customer's server was unsuspended and is online.",
}


def customer_observable_events(before: PanelSnapshot, after: PanelSnapshot) -> list[str]:
    events: list[str] = []

    if before.power_state != after.power_state:
        phrase = _POWER_PHRASES.get((before.power_state, after.power_state))
        events.append(phrase or f"The customer's server changed from "
                                f"{before.power_state} to {after.power_state}.")

    # New activity events with customer-visible consequences
    new_activity = after.activity[len(before.activity):]
    for ev in new_activity:
        if ev == "server:settings.reinstall":
            events.append(
                "The customer's server was just reinstalled — their files, "
                "world data, and mod configs are gone. They can see this."
            )

    # Deliberately NOT surfaced: startup command, variables, limits, file
    # edits. A customer cannot see those; the agent must not know them.
    return events
