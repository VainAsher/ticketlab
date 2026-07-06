"""OllamaLLM — renders customer prose via a local Ollama instance.

Research-informed design (R3): the `format` parameter grammar-constrains
generation but Ollama does NOT validate the finished output, so we validate
with Pydantic ourselves, retry once, then raise — the ConversationEngine
catches and falls back to ScriptedLLM. Temperature 0.2 for mild variety while
staying schema-stable.

The prompt gives the model ONLY: persona, ticket, thread, observable events,
and the facts the trainee has EARNED this turn. It never sees panel internals
or unearned facts — it cannot leak what it does not know, and its proposed
reveals/deltas are re-filtered by the engine anyway (defence in depth).
"""
from __future__ import annotations

import json
import os
import urllib.request

from pydantic import BaseModel, Field

from ticketlab.llm.base import TurnContext, LLMTurn


class _TurnSchema(BaseModel):
    reply: str = Field(max_length=1200)
    # Tone-only band, deliberately narrower than the engine's overall +/-25
    # clamp: objective progress (server back online, a fact earned) is
    # rewarded by the engine's own baseline_delta regardless of what this
    # model says, so this field is just "how did the AGENT'S MANNER make you
    # feel" — it should not carry the full emotional swing of the ticket.
    satisfaction_delta: int = Field(ge=-15, le=15)


class OllamaLLM:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: int = 45):
        self.base_url = (base_url or os.environ.get("TICKETLAB_OLLAMA_URL",
                                                    "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("TICKETLAB_OLLAMA_MODEL", "llama3.1")
        self.timeout = timeout

    def customer_turn(self, ctx: TurnContext) -> LLMTurn:
        prompt = self._build_prompt(ctx)
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": prompt["system"]},
                         *prompt["thread"],
                         {"role": "user", "content": prompt["user"]}],
            "format": _TurnSchema.model_json_schema(),
            "options": {"temperature": 0.2, "num_predict": 400},
        }
        last_err: Exception | None = None
        for _ in range(2):  # one retry on invalid JSON
            try:
                raw = self._post_chat(body)
                parsed = _TurnSchema.model_validate_json(raw)
                return LLMTurn(reply=parsed.reply, facts_revealed=[],
                               satisfaction_delta=parsed.satisfaction_delta)
            except Exception as e:  # noqa: BLE001 — any failure means fallback
                last_err = e
        raise ConnectionError(f"ollama turn failed after retry: {last_err}")

    def _post_chat(self, body: dict) -> str:
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data.get("message", {}).get("content", "")

    @staticmethod
    def _build_prompt(ctx: TurnContext) -> dict:
        system = (
            f"You are roleplaying {ctx.customer_name}, a game-server hosting "
            f"customer with persona '{ctx.persona}'. You opened this ticket:\n"
            f"Subject: {ctx.ticket_subject}\n{ctx.ticket_body}\n\n"
            "Rules: stay in character; never volunteer information beyond what "
            "is listed under EARNED FACTS; you only know what a customer could "
            "observe. Respond ONLY with JSON matching the given schema.\n\n"
            "satisfaction_delta (-15 to 15) reflects ONLY your reaction to the "
            "AGENT'S MANNER this turn — NOT whether your server is actually "
            "fixed, that is tracked separately and you should not try to "
            "reward or punish it here. Judge the reply on its own terms:\n"
            "+5 to +15: the agent asked a good diagnostic question, apologised "
            "sincerely, explained clearly, or gave a concrete next step.\n"
            "-5 to -15: the agent was dismissive, used unexplained jargon, "
            "blamed you, ignored what you just said, or promised something "
            "fixed it before you have any reason to believe them.\n"
            "-2 to +2: a neutral, administrative, or unremarkable reply.\n"
            "A frustrated persona being helped well should still trend "
            "positive — don't default to negative just to stay in character."
        )
        thread = [
            {"role": "assistant" if m["role"] == "customer" else "user",
             "content": m["text"]}
            for m in ctx.thread
        ]
        observable = "\n".join(f"- {e}" for e in ctx.state_events) or "- nothing new"
        earned = "\n".join(f"- {f}" for f in ctx.earned_facts) or "- none"
        user = (
            f"SUPPORT AGENT SAYS:\n{ctx.trainee_message}\n\n"
            f"THINGS YOU JUST NOTICED ABOUT YOUR SERVER:\n{observable}\n\n"
            f"EARNED FACTS you may now mention (the agent asked the right "
            f"question):\n{earned}\n\nReply as {ctx.customer_name}."
        )
        return {"system": system, "thread": thread, "user": user}
