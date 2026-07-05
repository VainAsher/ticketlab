"""Run TicketLab locally.

Binds 127.0.0.1 by default (REVIEW B2): there is no authentication in the MVP.
To expose beyond localhost, put it behind Traefik with Authentik forward-auth
and set TICKETLAB_HOST explicitly — that opt-in is deliberate friction.

Ollama customer mode: set TICKETLAB_OLLAMA=1 (plus TICKETLAB_OLLAMA_URL /
TICKETLAB_OLLAMA_MODEL if not localhost:11434 / llama3.1). Without it, the
deterministic scripted customer runs — no GPU needed for the demo.
"""
import os

import uvicorn

from ticketlab.api import create_app


def main() -> None:
    llm = grader = None
    if os.environ.get("TICKETLAB_OLLAMA") == "1":
        from ticketlab.llm.ollama import OllamaLLM
        from ticketlab.grader import OllamaGrader
        llm = OllamaLLM()
        grader = OllamaGrader()
    app = create_app(scenario_dir="scenarios", llm=llm, grader=grader,
                     demo_mode=True,
                     db_path=os.environ.get("TICKETLAB_DB", "ticketlab.db"))
    uvicorn.run(app,
                host=os.environ.get("TICKETLAB_HOST", "127.0.0.1"),
                port=int(os.environ.get("TICKETLAB_PORT", "8080")))


if __name__ == "__main__":
    main()
