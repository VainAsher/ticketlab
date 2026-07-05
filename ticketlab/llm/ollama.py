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
    satisfaction_delta: int = Field(ge=-25, le=25)


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
            "observe. Respond ONLY with JSON matching the given schema. "
            "satisfaction_delta reflects how this support reply made you feel "
            "(-25 to 25)."
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
