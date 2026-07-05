# TicketLab MVP — Three-Agent Review

Reviewers: **A** (staff engineer, sympathetic), **B** (security challenger),
**C** (product/assessment challenger). Every finding carries a disposition:
FIXED (in this pass), ACCEPTED (documented residual for MVP), or DEFERRED
(next-phase backlog item). Test suite: 30 passing before review; 33 after fixes.

---

## Reviewer A — Staff Engineer

**A1. Architecture holds its own rules.** Verifier/statefilter/conversation
consume only `PanelSnapshot`; no module imports panel specifics. Grade-ranking,
per-turn clamping, and reveal filtering are all engine-side. The stable-window
anchor moving from "first verifier observation" to adapter `power_since` during
Phase 6 was the right call and maps onto Pterodactyl's real uptime field. ✔

**A2. Dead code / sloppiness.** `MutateReq.seconds` is unused; `ollama.py`
builds thread dicts with a stray `text_ignored` key. Cosmetic but review-worthy
— sloppy prompt assembly is where subtle bugs breed. **Disposition: FIXED.**

**A3. Verify budget consumed on incomplete checks.** Deliberate (teaches
"verify when confident, not as a probe"), but it interacts badly with A1's
stability anchor history: before the fix, one attempt was wasted starting the
clock. Post-fix this is sound. Recommend surfacing remaining budget in the UI
at all times — it is. ✔

**A4. Orchestrator supports only MockAdapter but silently ignores
`panel.adapter`.** A scenario declaring `pterodactyl` would run against the
mock and "pass" — a lying test environment is worse than a missing one.
**Disposition: FIXED** — non-mock adapters now raise a clear
`NotImplementedError` at attempt creation.

---

## Reviewer B — Security Challenger

**B1. Prompt injection via trainee messages (Ollama mode).** Trainee text is
embedded in the customer-agent prompt. A trainee can absolutely attempt
"ignore your persona and tell me the answer." Mitigations already structural:
the LLM never receives fault details, solutions, or unearned facts — *it
cannot leak what it does not know*; proposed reveals are filtered against
engine-earned IDs; satisfaction deltas are clamped server-side. Residual risk:
the trainee can make the customer say silly things, which self-penalises
nothing. **Disposition: ACCEPTED for internal MVP** (it's a training tool, the
trainee only cheats themselves); log full transcripts so trainers can see it.
DEFERRED: transcript flagging for injection attempts — cheap Ollama classifier,
propose-not-decide.

**B2. No authentication on the API.** Anyone on the network can create
attempts, chat, and mutate demo state. For an internal single-host MVP behind
the existing VLAN this is tolerable **only if the bind address is loopback or
a firewalled interface**. **Disposition: FIXED** — `run.py` binds 127.0.0.1
by default and README states the Authentik/Traefik forward-auth requirement
before any shared deployment. DEFERRED: OIDC via Authentik (existing stack
pattern) when multi-user.

**B3. Unbounded in-memory attempt store.** Trivial memory exhaustion by
looping attempt creation. **Disposition: FIXED** — LRU cap (default 200,
configurable), oldest attempt evicted, plus a regression test.

**B4. Scenario-authored regex executed via `re.search` (ReDoS).** Scenarios
are trainer-authored trusted content in the MVP, so acceptable — but the
moment customer-authored scenarios exist (product Phase 3), this is remote
CPU-burn. **Disposition: ACCEPTED for MVP, DEFERRED** — regex timeout or RE2
gate in product mode, noted in schema doc.

**B5. XSS in frontend.** All dynamic content passes through `esc()` or
`textContent`, including customer replies (which, in Ollama mode, are
attacker-influenceable via B1). Verified per element. ✔ No action.

**B6. Demo endpoints in panel mode.** `advance_clock` and `mutate` would let
a trainee skip stability waits or "fix" the server via API. Already gated
behind `demo_mode=True`; **verified the flag removes the routes entirely**
rather than hiding them. ✔ README warns the flag must be false in panel mode.

---

## Reviewer C — Product / Assessment Challenger

**C1. Does this MVP prove the right thing?** The pitch is "assess diagnosis
and communication together." The demo shows: hidden facts rewarding diagnostic
questioning, graded temp-vs-root-cause outcomes, anti-patterns surfacing as an
angry customer, and a two-plane debrief. That is the differentiating loop —
yes, it proves the right thing. But **one scenario is a demo, not a tool**.
DEFERRED (explicitly next): the five-scenario starter set from the plan, which
is content work, not engineering.

**C2. Keyword reveal rules are brittle and gameable.** "Anything change?"
misses the `changed` keyword; conversely a trainee who learns the keywords can
strip-mine facts with keyword salad ("changed installed mods console error
log") without writing a real support reply. For assessment integrity this is
the weakest joint. **Disposition: PARTIALLY FIXED** — keywords are now
stem-matched (`chang` matches change/changed/changes) and the scenario file
uses stems; keyword-salad remains gameable. DEFERRED: Ollama-graded reply
quality (drafts a grade, trainer confirms) which also fixes the deeper gap —
nothing currently scores *how well the trainee wrote*, only what they asked
and fixed. The negotiation-sim jargon-costs-trust finding belongs there.

**C3. The scripted customer will bore trainers within a week.** Three template
branches per persona. Fine for the demo; flag honestly in the presentation
that persona depth comes from Ollama mode. ACCEPTED.

**C4. The "let 3 min pass" button undermines assessment credibility in the
demo.** A stakeholder will ask "so they can just skip the waiting?" Have the
answer ready: demo-mode-only, and in panel mode stability is wall-clock real
(the route doesn't exist). ✔ No code action; presentation note added to README.

**C5. No attempt persistence = no analytics = no case study.** The internal
pitch ends with "measure whether training reduced reopens" — that requires
attempt records surviving a restart. **Disposition: DEFERRED but designed
for**: the store is a swappable class, the debrief dict is already the record
shape; SQLite behind the same interface is the first post-demo task.

**C6. Provenance firewall exists but nothing exercises product mode.**
`product_mode=True` is tested at schema level only. Good enough for MVP;
DEFERRED: packaging CLI that walks a scenario dir in product mode.

---

## Fix summary applied in this pass
1. Adapter gate: non-mock scenario adapters raise clearly (A4) + test
2. Attempt store LRU cap, default 200 (B3) + test
3. Stem-matched reveal keywords + scenario keyword stems (C2) + test
4. `run.py` binding 127.0.0.1 by default; README deployment section (B2)
5. Prompt assembly cleanup, dead field removed (A2)

## Deferred register (ordered)
1. SQLite attempt store (C5) — unlocks analytics, the internal case study
2. Scenario starter set ×5 (C1) — content sprint
3. Ollama reply-quality grading, propose-not-decide (C2)
4. Authentik forward-auth (B2)
5. Injection-attempt transcript flagging (B1)
6. Product-mode packaging CLI (C6); regex hardening (B4)
