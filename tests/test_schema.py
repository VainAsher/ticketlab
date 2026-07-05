"""Phase 1: scenario schema — tests written before ticketlab/schema.py exists."""
import pytest
from pathlib import Path

SCEN = Path(__file__).parent.parent / "scenarios" / "oom-crash-loop-modded.yaml"


def test_example_scenario_loads():
    from ticketlab.schema import load_scenario
    s = load_scenario(SCEN)
    assert s.metadata.id == "oom-crash-loop-modded"
    assert s.metadata.provenance.source == "generic"
    assert s.panel.adapter == "mock"


def test_solutions_are_graded_and_ordered():
    from ticketlab.schema import load_scenario
    s = load_scenario(SCEN)
    grades = {sol.id: sol.grade for sol in s.verification.solutions}
    assert grades["right-size-heap"] == "full"
    assert grades["raise-container-limit"] == "temp"
    # full must outrank temp when both match
    from ticketlab.schema import GRADE_RANK
    assert GRADE_RANK["full"] > GRADE_RANK["temp"]


def test_hidden_facts_present_with_reveal_keywords():
    from ticketlab.schema import load_scenario
    s = load_scenario(SCEN)
    facts = {f.id: f for f in s.conversation.hidden_facts}
    assert "added-mods" in facts
    assert facts["added-mods"].reveal_keywords  # non-empty keyword list


def test_internal_provenance_rejected_when_packaging_flag_set():
    """The IP firewall: source=internal scenarios refuse to load in product mode."""
    from ticketlab.schema import load_scenario, ProvenanceError
    import yaml
    raw = yaml.safe_load(SCEN.read_text())
    raw["metadata"]["provenance"]["source"] = "internal"
    with pytest.raises(ProvenanceError):
        load_scenario_dict = __import__("ticketlab.schema", fromlist=["load_scenario_from_dict"])
        load_scenario_dict.load_scenario_from_dict(raw, product_mode=True)


def test_unknown_fault_verb_rejected():
    from ticketlab.schema import load_scenario_from_dict
    import yaml
    raw = yaml.safe_load(SCEN.read_text())
    raw["fault"]["steps"].append({"action": "rm_dash_rf", "value": "/"})
    with pytest.raises(Exception):
        load_scenario_from_dict(raw)


def test_satisfaction_bounds_validated():
    from ticketlab.schema import load_scenario_from_dict
    import yaml
    raw = yaml.safe_load(SCEN.read_text())
    raw["conversation"]["satisfaction_start"] = 250
    with pytest.raises(Exception):
        load_scenario_from_dict(raw)
