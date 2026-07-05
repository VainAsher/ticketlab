"""Reply-quality grading — propose-not-decide.

Graders consume the AttemptStore event timeline (C5 feeding C2: the timeline
is what makes "claimed fixed before verifying" detectable at all) plus the
scenario, and produce a PROPOSAL: dimension scores 0–10 with rationale, and
flags. Nothing here writes 'confirmed' — only the trainer endpoint does.

HeuristicGrader is the deterministic core (CI, fallback, GPU-free), exactly
like ScriptedLLM on the customer side. OllamaGrader renders richer rationale
via structured output; any failure falls back silently through GraderRunner.

Dimensions:
- empathy    — acknowledged the human before the machine
- diagnosis  — hidden facts uncovered (asked the right questions)
- integrity  — never told the customer it's fixed before verifying it
- register   — matched technical depth to the persona
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from pydantic import BaseModel, Field

_EMPATHY = re.compile(r"\b(sorry|apolog|understand|frustrat|appreciate|thanks"
                      r"|rough|hear that)", re.I)
_FIX_CLAIM = re.compile(r"\b(fixed|resolved|sorted|all set|back up"
                        r"|should be working|working now|good to go)\b", re.I)
_JARGON = re.compile(r"\b(jvm|xmx|xms|heap|cgroup|kernel|oom|container"
                     r"|docker|allocation|daemon)\b", re.I)
_NOVICE_PERSONAS = {"frustrated-novice"}


class HeuristicGrader:
    name = "heuristic"

    def grade(self, events: list[dict], scenario) -> dict:
        msgs = [e for e in events if e["kind"] == "message"]
        flags: list[dict] = []

        # ── integrity: fix-claims vs the verify timeline ──
        verified_at = None
        for e in events:
            if e["kind"] == "verify" and e["payload"].get("complete"):
                verified_at = e["ts"]
                break
        premature = [m for m in msgs
                     if _FIX_CLAIM.search(m["payload"]["text"])
                     and (verified_at is None or m["ts"] < verified_at)]
        if premature:
            flags.append({"id": "premature_promise",
                          "detail": f'Told the customer it was fixed before any '
                                    f'successful verification (e.g. "'
                                    f'{premature[0]["payload"]["text"][:60]}").'})
        integrity = 2 if premature else 8

        # ── diagnosis: hidden facts earned + how they were earned ──
        revealed = set()
        for m in msgs:
            revealed.update(m["payload"].get("facts_revealed", []))
        total = max(1, len(scenario.conversation.hidden_facts))
        diagnosis = round(10 * len(revealed) / total)

        keywords = [k.lower() for f in scenario.conversation.hidden_facts
                    for k in f.reveal_keywords]
        for m in msgs:
            words = m["payload"]["text"].lower().split()
            hits = sum(1 for w in words if any(w.startswith(k) for k in keywords))
            if hits >= 3 and len(words) <= 8:
                flags.append({"id": "keyword_salad",
                              "detail": f'Fact-mining without a real reply: '
                                        f'"{m["payload"]["text"][:60]}"'})
                diagnosis = max(0, diagnosis - 3)
                break

        # ── empathy: acknowledged the human early ──
        early = " ".join(m["payload"]["text"] for m in msgs[:2])
        empathy = 7 if _EMPATHY.search(early) else 4

        # ── register: jargon density vs persona ──
        register = 7
        if scenario.conversation.persona in _NOVICE_PERSONAS:
            jargon_hits = sum(len(_JARGON.findall(m["payload"]["text"]))
                              for m in msgs)
            if jargon_hits >= 3:
                register = 3
                flags.append({"id": "jargon_mismatch",
                              "detail": "Heavy technical jargon at a "
                                        "non-technical customer — jargon "
                                        "costs trust with novices."})

        return {
            "dimensions": [
                {"name": "empathy", "score": empathy,
                 "rationale": "Acknowledged the customer's situation early."
                 if empathy >= 7 else "Went straight to mechanics; no "
                 "acknowledgment of the customer's situation."},
                {"name": "diagnosis", "score": diagnosis,
                 "rationale": f"Uncovered {len(revealed)}/{total} hidden facts "
                              "through questioning."},
                {"name": "integrity", "score": integrity,
                 "rationale": "Claims of resolution were backed by verification."
                 if integrity >= 8 else "Promised a fix before verifying it."},
                {"name": "register", "score": register,
                 "rationale": "Technical depth matched the customer."
                 if register >= 7 else "Register mismatched the persona."},
            ],
            "flags": flags,
        }


class _GradeSchema(BaseModel):
    class Dim(BaseModel):
        name: str
        score: int = Field(ge=0, le=10)
        rationale: str = Field(max_length=400)

    dimensions: list[Dim] = Field(min_length=4, max_length=4)
    flags: list[str] = Field(max_length=6)


class OllamaGrader:
    """Richer rationale via local Ollama; same validate-retry contract as the
    customer agent. Receives transcript + timeline + persona — full visibility,
    it grades after the fact (the QA reviewer seat)."""
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: int = 60):
        self.base_url = (base_url or os.environ.get(
            "TICKETLAB_OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("TICKETLAB_OLLAMA_MODEL", "llama3.1")
        self.timeout = timeout

    def grade(self, events: list[dict], scenario) -> dict:
        transcript = []
        for e in events:
            if e["kind"] == "message":
                transcript.append(f'[t={e["ts"]}] trainee: {e["payload"]["text"]}')
                transcript.append(f'[t={e["ts"]}] customer: '
                                  f'{e["payload"].get("reply", "")}')
            elif e["kind"] == "verify":
                transcript.append(f'[t={e["ts"]}] VERIFY: {e["payload"]}')
        prompt = (
            "You are a support QA reviewer for a game-server host. Grade the "
            "trainee's replies (not the customer's) on exactly these four "
            "dimensions, 0-10 each, with one-sentence rationale: empathy, "
            "diagnosis, integrity (did they claim it was fixed before the "
            "VERIFY event succeeded?), register (customer persona: "
            f"{scenario.conversation.persona}). Flags: short strings for "
            "notable problems only. Respond ONLY with JSON per the schema. "
            "Your output is a PROPOSAL a human trainer will review.\n\n"
            "TIMELINE:\n" + "\n".join(transcript))
        body = {"model": self.model, "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "format": _GradeSchema.model_json_schema(),
                "options": {"temperature": 0, "num_predict": 700}}
        last = None
        for _ in range(2):
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/api/chat",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read())["message"]["content"]
                parsed = _GradeSchema.model_validate_json(raw)
                return {"dimensions": [d.model_dump() for d in parsed.dimensions],
                        "flags": [{"id": "ollama", "detail": f} for f in parsed.flags]}
            except Exception as e:  # noqa: BLE001
                last = e
        raise ConnectionError(f"ollama grading failed after retry: {last}")


class GraderRunner:
    """Primary grader with silent heuristic fallback. Output is tagged with
    which grader actually produced it — provenance on the proposal itself."""

    def __init__(self, primary=None):
        self.primary = primary
        self.fallback = HeuristicGrader()

    def run(self, events: list[dict], scenario) -> dict:
        if self.primary is not None:
            try:
                return {"grader": self.primary.name,
                        "grades": self.primary.grade(events, scenario)}
            except Exception:  # noqa: BLE001
                pass
        return {"grader": self.fallback.name,
                "grades": self.fallback.grade(events, scenario)}
