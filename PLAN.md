# TicketLab — Phased Plan for Internal MVP
**Support-event simulation labs for game-panel support training.**
Target: working, configurable prototype presentable internally at BisectHosting.
Governed by VAS Engineering Framework conventions: adapter/ACL boundaries,
fixture-first testing, propose-not-decide AI verbs, provenance stamping.

## Research synthesis (fan-out findings)

**R1 — Panel ecosystem is fragmenting, adapter layer is mandatory.**
Pterodactyl resumed releases (Wings 1.12.1 Jan 2026, panel 1.12.2 Mar 2026) but
Pelican ships faster and new Rust panels (Calagopus, Catalyst) are emerging.
No scenario or verifier code may touch a panel API directly — everything goes
through `PanelAdapter`. Mock adapter ships first; Pterodactyl adapter is a thin
HTTP implementation of the same protocol; Pelican inherits most of it (same API
surface, per their FAQ).

**R2 — Required panel capabilities all exist in Pterodactyl's client API.**
`GET /api/client/servers/{id}` (state), `/startup` (variables + startup command),
`/activity` (audit log — powers anti-pattern detection), power actions, and
build limits via the application API. Rate limit 240 req/min/key — polling at
10–15s intervals per attempt is comfortably inside budget.

**R3 — Ollama structured outputs are grammar-constrained but not validated.**
`format` accepts a JSON schema (Pydantic `model_json_schema()`), temperature 0
recommended. Ollama does NOT verify the finished response parses — truncation
mid-JSON is possible. Therefore: Pydantic validation on our side, one retry,
then fall back to the scripted engine. The scripted engine is not a stub — it
is the deterministic core that tests run against and the demo can run on with
no GPU in the room.

**R4 — LLM must never hold state or decide completion.**
Hidden-fact reveal state, satisfaction score, and scenario completion are all
orchestrator-owned. The LLM renders prose from state; it never mutates it
directly (its proposed `satisfaction_delta` is clamped and its `facts_revealed`
are filtered against reveal rules before acceptance).

## Architecture (bounded for MVP)

```
frontend/index.html (static, served by FastAPI)
        │ REST
ticketlab.api (FastAPI)
        │
ticketlab.orchestrator ── AttemptStore (in-memory, SQLite-shaped dict for MVP)
   │            │                │
verifier   conversation      statefilter
   │        │       │            │
PanelAdapter   LLMClient    (customer-observable diff)
   │                │
 mock / pterodactyl   scripted / ollama
```

Scope fences (explicitly OUT of MVP): auth (internal tool behind Tailscale/VLAN),
Postgres, multi-tenant, scenario builder UI, real Ollama in CI, escalation
grading, hint system, WebSocket console capture.

## Phases

| Phase | Deliverable | Gate (must pass to proceed) |
|---|---|---|
| 0 | Repo scaffold, deps, this plan | — |
| 1 | Scenario schema as Pydantic models; loads v0.2 YAML | Schema tests green; example scenario round-trips |
| 2 | PanelAdapter protocol + MockAdapter with mutable state, activity log, injectable clock | Adapter tests green |
| 3 | Verifier: graded solutions, `stable_for_seconds`, anti-patterns, highest-grade-wins | Verifier tests green incl. crash-loop flicker case |
| 4 | State filter: customer-observable diffs from adapter snapshots | Filter tests green (customer never sees startup command) |
| 5 | Conversation engine: hidden facts, satisfaction, scripted LLM; Ollama client behind same protocol with validate-retry-fallback | Conversation tests green with zero network |
| 6 | Orchestrator + FastAPI API + demo mutation endpoints | API tests green end-to-end (attempt → chat → fix → verify → debrief) |
| 7 | Frontend: ticket thread, satisfaction meter, demo controls, debrief | Manual smoke via TestClient-rendered HTML |
| 8 | Three-agent review (1 staff-engineer, 2 challengers), fixes applied | REVIEW.md with disposition per finding |

## Test-led rule
Every phase writes its test file before its implementation module. The mock
adapter and scripted LLM are the fixtures — no test may require a panel,
network, or GPU. Time is injected (`Clock` protocol) so `stable_for_seconds`
is tested in milliseconds.

## Deployment target (post-MVP, for the internal presentation)
Ubuntu 24.04 LXC or VM on the Proxmox cluster, Docker via Coolify, Traefik in
front, Ollama on existing GPU host, panel = dedicated training Pterodactyl
instance (never production). All open source; no external services.
