"""ScriptedLLM — deterministic customer engine. Not a stub: it is the CI
fixture and the fallback when Ollama is unreachable, and it can run the whole
demo on a laptop with no GPU. Persona flavour is template-based; all state
decisions live in the engine regardless of which LLM renders prose.
"""
from __future__ import annotations

from ticketlab.llm.base import TurnContext, LLMTurn

_PERSONA_OPENERS = {
    "technical-but-wrong": {
        "neutral": "Look, I know my way around this stuff — ",
        "pleased": "Okay, that's more like it. ",
        "annoyed": "I've explained this already. ",
    },
    "frustrated-novice": {
        "neutral": "I really don't understand any of this — ",
        "pleased": "Oh thank goodness. ",
        "annoyed": "This is exactly why I'm so fed up. ",
    },
    "anxious-planner": {
        "neutral": "Sorry to keep asking, I just want to be sure — ",
        "pleased": "Oh, that's such a relief to hear. ",
        "annoyed": "I already said I'm worried about the timing on this. ",
    },
}


class ScriptedLLM:
    """Tone only: the numeric reward for state changes and earned facts is
    the engine's `_baseline_delta` (same signal for every LLM backend), so
    this class just picks phrasing and a small mood nudge for the case
    nothing observable happened at all."""

    def customer_turn(self, ctx: TurnContext) -> LLMTurn:
        tone = 0
        parts: list[str] = []

        online = any("online" in e.lower() for e in ctx.state_events)
        data_loss = any("reinstall" in e.lower() or "data" in e.lower()
                        for e in ctx.state_events)
        offline = any("offline" in e.lower() for e in ctx.state_events)

        if data_loss:
            parts.append("Hang on — my files are GONE. Did you just reinstall my "
                         "server?! All my mod configs are wiped.")
        if online:
            parts.append("Okay... it's actually staying up now. What did you do?")
        if offline:
            parts.append("And now it's gone down again.")
        if ctx.earned_facts:
            for fact in ctx.earned_facts:
                parts.append(f"Now that you mention it — {fact.lower()}.")

        if not parts:
            # nothing observable changed and no fact earned: persona grumble
            tone = -2
            parts.append("Right, but is anyone actually going to FIX it? "
                         "I still think it's your hardware.")

        pleased = (online or ctx.earned_facts) and not data_loss
        mood = "pleased" if pleased else "annoyed" if (data_loss or offline or tone < 0) else "neutral"
        opener = _PERSONA_OPENERS.get(ctx.persona, {}).get(mood, "")
        return LLMTurn(reply=opener + " ".join(parts),
                       facts_revealed=[],   # scripted path: engine already decided
                       satisfaction_delta=tone)
