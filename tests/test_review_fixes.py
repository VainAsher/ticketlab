"""Regression tests for REVIEW.md findings A4, B3, C2."""
from pathlib import Path

import pytest

SCEN = Path(__file__).parent.parent / "scenarios" / "oom-crash-loop-modded.yaml"


def test_a4_non_mock_adapter_refuses_loudly():
    from ticketlab.schema import load_scenario
    from ticketlab.orchestrator import Orchestrator
    s = load_scenario(SCEN)
    s = s.model_copy(update={"panel": s.panel.model_copy(update={"adapter": "pterodactyl"})})
    with pytest.raises(NotImplementedError):
        Orchestrator().create_attempt(s)


def test_b3_attempt_store_capped_and_evicts_oldest():
    from ticketlab.schema import load_scenario
    from ticketlab.orchestrator import Orchestrator
    s = load_scenario(SCEN)
    orch = Orchestrator(max_attempts=3)
    ids = [orch.create_attempt(s).id for _ in range(5)]
    assert orch.get(ids[0]) is None and orch.get(ids[1]) is None  # evicted
    assert all(orch.get(i) for i in ids[2:])                      # survivors


def test_c2_stem_keywords_match_inflections():
    from ticketlab.schema import load_scenario
    from ticketlab.conversation import ConversationEngine
    s = load_scenario(SCEN)
    eng = ConversationEngine(s)
    turn = eng.trainee_message("Did anything change on your side lately?",
                               state_events=[])
    assert "added-mods" in turn.facts_revealed   # 'chang' stem hits 'change'
