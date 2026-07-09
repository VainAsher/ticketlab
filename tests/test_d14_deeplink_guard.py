"""D14 (2026-07-09 decisions round): the platform-gateway trainer dashboard
links into this trainer console per scenario. That needs the console to
honor hash deep-links on load:

  /trainer#scenario=<id>   preselect the scenario filter, then load rows
  /trainer#attempt=<id>    open the attempt dossier drawer directly

The console is a single static page (frontend/trainer.html) with no build
step, so - like the Biko Bot grep-guard - these are source guards pinning
the deep-link wiring into the init sequence. The behavior itself is
browser-verified (the init block is plain DOM + fetch; there is no JS test
runner in this repo to execute it).

Minimal-invasive by design: only the /* init */ IIFE changes; loadStats /
loadRows / openDetail are called exactly as the existing UI does.
"""
from pathlib import Path

import pytest

TRAINER_HTML = Path(__file__).resolve().parent.parent / "frontend" / "trainer.html"


@pytest.fixture(scope="module")
def html() -> str:
    return TRAINER_HTML.read_text(encoding="utf-8")


def test_init_parses_hash_params(html: str) -> None:
    assert "location.hash" in html, (
        "trainer.html init must parse location.hash for deep-link params "
        "(gateway dashboard links arrive as /trainer#scenario=<id>)"
    )


def test_scenario_hash_preselects_filter_before_rows_load(html: str) -> None:
    assert "h.get('scenario')" in html, (
        "#scenario=<id> must preselect the fScen filter"
    )
    # the preselect is useless unless it happens after options exist
    # (loadStats populates fScen) and before the row query runs
    init = html[html.index("/* init"):]
    assert init.index("await loadStats()") < init.index("h.get('scenario')") < init.index(
        "await loadRows()"
    ), "init order must be loadStats -> preselect fScen -> loadRows"


def test_attempt_hash_opens_dossier(html: str) -> None:
    assert "openDetail(h.get('attempt'))" in html, (
        "#attempt=<id> must open the attempt dossier drawer via the same "
        "openDetail() the row click uses"
    )
