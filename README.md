# TicketLab — support event simulator (internal MVP)

Trains and assesses game-hosting support staff on **both planes of a support
event**: what they fixed (technical) and what they said (conversational).
A simulated customer with hidden facts rewards diagnostic questioning; a
graded verifier distinguishes root-cause fixes from temp fixes; destructive
"fixes" surface as an angry customer, not just a score deduction.

## Quickstart (Ubuntu, Python 3.12)
```bash
pip install fastapi pydantic pyyaml uvicorn pytest httpx --break-system-packages
python3 -m pytest tests/ -q     # 33 tests, no network, no GPU
python3 run.py                  # http://127.0.0.1:8080
```
Demo arc: Start attempt → ask the customer a diagnostic question (try asking
what changed recently) → fix the startup command in the mock panel (Xmx must
fit the 1024M limit) → Start → "Let 3 min pass" → Check my work → Debrief.
Try the temp fix (raise memory to 5120) or the reinstall for contrast.

## v0.2 additions (deferred register items 1–3, delivered)
- **SQLite attempt store** (`ticketlab/store.py`, stdlib sqlite3): append-only
  event timeline + finalized records + grades; survives restarts;
  `GET /analytics/summary` gives per-scenario aggregates — the numbers for the
  internal case study. Set `TICKETLAB_DB` to place the file (Docker volume).
- **Scenario starter set ×5**: OOM crash loop (graded temp-vs-root showcase),
  copied JVM flags, wrong Java version (confidently-wrong customer), suspended
  ≠ broken (soft-skills/billing), startup variable typo. Scenarios now carry
  declarative `mock_physics` crash rules; the contract test in
  tests/test_scenarios.py parametrizes over the directory, so a badly authored
  scenario fails CI, not a lab session.
- **Reply-quality grading, propose-not-decide** (`ticketlab/grader.py`):
  four dimensions (empathy, diagnosis, integrity, register) + flags
  (premature promise — claimed fixed before a successful verify, detected
  from the event timeline; keyword salad; jargon-vs-persona). HeuristicGrader
  is the deterministic core; OllamaGrader adds rationale and falls back
  silently. Every AI grade lands `status: proposed`; only
  `POST /attempts/{id}/grades/confirm` (a human) writes `confirmed`, with
  per-dimension overrides recorded as such.

## What's in the box
- `PLAN.md` — research synthesis + phased plan with gates
- `REVIEW.md` — three-agent review (1 staff engineer, 2 challengers),
  disposition per finding, deferred register
- `ticketlab/` — schema (Pydantic, provenance firewall, verb whitelist),
  adapters (protocol + mock with OOM physics + fake clock), verifier (graded
  solutions, uptime-anchored stability, historical anti-patterns), state
  filter (customer-observable diffs), conversation engine (orchestrator-owned
  facts/satisfaction, LLM as renderer), Ollama client (structured output,
  validate-retry-fallback), orchestrator, FastAPI surface
- `scenarios/` — v2 scenario YAML (one scenario; starter set of five is the
  first content sprint)
- `frontend/` — single-file UI in the VAS visual language

## Design invariants (worth repeating in the room)
1. **Completion is a state machine, never an LLM judgment.**
2. **The customer agent only knows what a customer could see** — it cannot
   leak fault details or solutions because it never receives them.
3. **The LLM proposes; the engine decides** — reveals filtered, deltas clamped.
4. **Scripted mode is a first-class engine**, not a stub: CI, fallback, and
   GPU-free demos all run on it.
5. **Provenance firewall**: `source: internal` content can never ship in
   product mode; enforced in code, tested.

## Deployment (internal, post-demo)
Ubuntu 24.04 VM/LXC on Proxmox → Docker via Coolify → Traefik with Authentik
forward-auth (there is NO built-in auth) → Ollama on the existing GPU host.
`demo_mode=False` in panel mode removes the mutate/clock routes entirely; the
trainee acts in a real (training-only) Pterodactyl instance instead, behind
the same-origin iframe pattern.

## Presentation FAQ ammunition
- *"Can they skip the waiting?"* Demo-only. In panel mode the clock endpoint
  does not exist and stability is wall-clock against real uptime.
- *"What if Ollama is down mid-session?"* Scripted fallback engages per turn;
  the attempt never dies.
- *"Can a trainee prompt-inject the customer?"* They can make it say silly
  things; they cannot extract solutions (never in the prompt), force reveals
  (engine-filtered), or move satisfaction beyond clamps. Transcripts are the
  trainer's audit trail.
- *"Where does Zendesk fit?"* Scenario *selection* (which failure modes to
  author, from ticket categories/reopens) — with sign-off, PII-scrubbed,
  provenance-stamped internal. Never raw ticket content in scenarios.
