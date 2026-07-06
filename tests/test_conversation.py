"""Phase 5: conversation engine. Written before ticketlab/conversation.py.

Invariants:
- hidden facts reveal ONLY when the trainee's message hits reveal keywords
- the LLM's proposed reveals are filtered against orchestrator rules (an LLM
  cannot leak a fact the trainee didn't earn)
- satisfaction is clamped and orchestrator-owned
- satisfaction floor triggers the escalation fail-state
- Ollama failure falls back to scripted engine (no exception escapes)
"""
from pathlib import Path

SCEN = Path(__file__).parent.parent / "scenarios" / "oom-crash-loop-modded.yaml"


def make_engine(llm=None):
    from ticketlab.schema import load_scenario
    from ticketlab.conversation import ConversationEngine
    from ticketlab.llm.scripted import ScriptedLLM
    s = load_scenario(SCEN)
    return s, ConversationEngine(s, llm=llm or ScriptedLLM())


def test_generic_reply_reveals_nothing():
    s, eng = make_engine()
    turn = eng.trainee_message("Hi, sorry to hear that! Let me look into it.", state_events=[])
    assert turn.facts_revealed == []
    assert eng.state.revealed == set()


def test_diagnostic_question_reveals_matching_fact():
    s, eng = make_engine()
    turn = eng.trainee_message(
        "Has anything changed recently — any new mods installed?", state_events=[])
    assert "added-mods" in turn.facts_revealed
    assert "added-mods" in eng.state.revealed


def test_reveal_is_idempotent():
    s, eng = make_engine()
    eng.trainee_message("Anything changed recently?", state_events=[])
    turn2 = eng.trainee_message("And again — anything changed recently?", state_events=[])
    assert "added-mods" not in turn2.facts_revealed  # already revealed


def test_llm_cannot_leak_unearned_fact():
    """A misbehaving LLM proposing a fact the keywords didn't earn is filtered."""
    from ticketlab.llm.base import LLMTurn

    class LeakyLLM:
        def customer_turn(self, ctx):
            return LLMTurn(reply="btw I installed 30 mods lol",
                           facts_revealed=["added-mods", "ignored-warning"],
                           satisfaction_delta=0)

    s, eng = make_engine(llm=LeakyLLM())
    turn = eng.trainee_message("Hello!", state_events=[])   # no keywords hit
    assert turn.facts_revealed == []


def test_satisfaction_clamped_and_floor_triggers_escalation():
    from ticketlab.llm.base import LLMTurn

    class AngryLLM:
        def customer_turn(self, ctx):
            return LLMTurn(reply="THIS IS UNACCEPTABLE", facts_revealed=[],
                           satisfaction_delta=-500)

    s, eng = make_engine(llm=AngryLLM())
    turn1 = eng.trainee_message("no refunds, deal with it", state_events=[])
    assert turn1.satisfaction_delta == -25      # -500 clamped to per-turn bound
    assert eng.state.satisfaction == 15         # 40 - 25; one bad reply can't end it
    assert eng.state.escalated is False
    eng.trainee_message("stop whining", state_events=[])
    assert eng.state.satisfaction == 0          # clamped at floor, not negative
    assert eng.state.escalated is True          # floor fail-state
    # post-escalation messages get the tombstone reply
    turn3 = eng.trainee_message("hello?", state_events=[])
    assert "escalated" in turn3.reply.lower()


def test_server_online_event_improves_mood_in_scripted_engine():
    s, eng = make_engine()
    turn = eng.trainee_message(
        "I've made a change, could you check?",
        state_events=["The customer's server has just come online."])
    assert turn.satisfaction_delta > 0


def test_earned_fact_guarantees_positive_floor_even_on_sour_llm_tone():
    """A trainee who asks the right diagnostic question should see SOME
    credit even if the LLM's tone judgement that turn is negative — this is
    the engine-owned baseline, independent of what any LLM decides."""
    from ticketlab.llm.base import LLMTurn

    class GrumpyLLM:
        def customer_turn(self, ctx):
            return LLMTurn(reply="hmph", facts_revealed=[],
                           satisfaction_delta=-3)   # sour tone, in-range

    s, eng = make_engine(llm=GrumpyLLM())
    start = eng.state.satisfaction
    turn = eng.trainee_message("Anything changed recently, new mods installed?",
                               state_events=[])
    assert "added-mods" in turn.facts_revealed
    # baseline +5 for the earned fact outweighs the -3 tone
    assert turn.satisfaction_delta == 2
    assert eng.state.satisfaction == start + 2


def test_server_online_rewarded_regardless_of_llm_tone():
    from ticketlab.llm.base import LLMTurn

    class NeutralLLM:
        def customer_turn(self, ctx):
            return LLMTurn(reply="ok", facts_revealed=[], satisfaction_delta=0)

    s, eng = make_engine(llm=NeutralLLM())
    turn = eng.trainee_message("fixed it, check now",
                               state_events=["The server has come back online."])
    assert turn.satisfaction_delta == 15   # baseline only, LLM contributed 0


def test_ollama_failure_falls_back_to_scripted():
    class ExplodingLLM:
        def customer_turn(self, ctx):
            raise ConnectionError("ollama down")

    s, eng = make_engine(llm=ExplodingLLM())
    turn = eng.trainee_message("Anything changed recently?", state_events=[])
    assert turn.reply                          # got a reply anyway
    assert "added-mods" in turn.facts_revealed  # scripted path still ran rules
