# TicketLab → homelab migration status

Variables (decided 2026-07-05):

| Var | Value |
|---|---|
| APP_NAME | ticketlab |
| SUBDOMAIN | ticketlab.vainasherstudios.com |
| SUBDOMAIN_LABEL | ticketlab |
| APP_PORT | 8090 (free on .42; taken: 3000-3002, 8000, 8100, 8501, 8765) |
| NEEDS_OLLAMA | yes — TICKETLAB_OLLAMA=1 |
| MODEL_ENV | TICKETLAB_OLLAMA_URL / TICKETLAB_OLLAMA_MODEL = llama3.1:8b-instruct-q5_K_M |
| DB_PATH_ENV | TICKETLAB_DB = /data/ticketlab.db (named volume ticketlab-data) |
| Data decision | start clean — do NOT ship local ticketlab.db |
| Health check | GET /scenarios (no dedicated /healthz) |

Steps:

- [x] Step 0 — Pre-flight: 66 tests pass locally; VM ports enumerated; env vars grepped (TICKETLAB_HOST/PORT/DB/OLLAMA/OLLAMA_URL/OLLAMA_MODEL — Ollama client is stdlib urllib, runtime deps only fastapi/uvicorn/pydantic/pyyaml)
- [x] Step 1 — Containerize: deploy/Dockerfile, .dockerignore, docker-compose.yml, .env.example, requirements.txt written
- [x] Step 1b — Local `docker compose build` passes; smoke test 200 on /scenarios and / (note: test env vars from Git Bash need MSYS_NO_PATHCONV=1 or paths get mangled)
- [x] Step 2 — Tarball → scp → /opt/ticketlab on .42; .env = .env.example values; `up -d --build`; clean startup on :8090, clean DB
- [x] Step 3 — Ollama: llama3.1:8b-instruct-q5_K_M confirmed reachable from the VM at 192.168.0.127:11434
- [x] Step 4 — DNS: CNAME created via `cloudflared tunnel route dns` on .23; recorded in cloudflare-dns-records.yml (homelab-infrastructure commit c8a69d8)
- [x] Step 5 — Tunnel ingress added to live /etc/cloudflared/config.yml (backup: config.yml.bak-ticketlab-*), validated, cloudflared restarted healthy
- [x] Step 6 — Traefik: ~/infra-core/traefik/dynamic/app-ticketlab.yml (actual path has no docker/ segment); cross-VM curl from .23 → .42:8090 = 200
- [x] Step 7 — Authentik: provider pk=24, application slug=ticketlab, outpost patched via read-modify-write. No policy binding: NO app in this instance restricts by group (house pattern = any authenticated user). Forward-auth 302 matches reply-workbench.
- [x] Step 8a — Public https 302 → Authentik login page shows "continue to TicketLab"; valid cert
- [x] Step 8b — Restart-survival: attempt created pre-restart visible in /analytics/summary post-restart (volume ticketlab-data)
- [ ] Step 8c — Post-login browser check: click a real UI control, console clean (BLOCKED on user logging in — credential entry is user-only)
- [x] Step 8d — Record: no ENVIRONMENT.md exists in the homelab-infrastructure clone; the DNS vars commit is the IaC record. Wiki.js entry = manual follow-up.
