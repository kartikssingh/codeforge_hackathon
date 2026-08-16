"""The deterministic triage engine — ARIA's behaviour with no models at all."""

from __future__ import annotations

import pytest

from aria.agents.rules import rule_engine
from aria.domain.enums import Severity
from aria.utils.textutil import fuzzy_best_match, keyword_hits, normalise, similarity


def _top(text: str):
    matches = rule_engine.match(text)
    assert matches, f"no rule matched: {text!r}"
    return matches[0]


def test_catalogue_loads():
    assert len(rule_engine) >= 20


@pytest.mark.parametrize(
    "report,expected_id",
    [
        ("He collapsed and is not breathing, there is no pulse", "cardiac-arrest"),
        ("Her face is drooping and her speech is slurred", "stroke"),
        ("The wound is bleeding heavily and won't stop", "severe-bleeding"),
        ("A child is choking on food and can't breathe", "choking"),
        ("He was stung and his throat is closing, we need an epipen", "anaphylaxis"),
        ("Elderly woman fell, her leg looks wrong and she cannot walk", "fracture"),
        ("Boiling water spilled, she is burnt on the arm", "burns"),
        ("He is diabetic and his blood sugar has crashed", "diabetic-emergency"),
        ("Two people are trapped under rubble after the collapse", "trapped-structure"),
        ("Family of four needs food and water, nothing to eat", "supply-request"),
    ],
)
def test_reports_route_to_the_right_protocol(report, expected_id):
    assert _top(report).rule.id == expected_id


def test_severity_is_carried_from_the_rule():
    assert _top("no pulse and not breathing").rule.severity is Severity.CRITICAL
    assert _top("small cut on the hand needs a bandage").rule.severity is Severity.LOW


def test_confidence_grows_with_evidence():
    weak = _top("she has a headache and feels dizzy").confidence
    strong = _top(
        "he is dehydrated, no water for two days, dizzy with cramps and a headache"
    ).confidence
    assert strong > weak


def test_situations_carry_materials_instructions_and_citations():
    situations = rule_engine.situations("He collapsed, no pulse, not breathing")
    top = situations[0]

    assert top.origin == "rules"
    assert [m.item for m in top.materials] == ["AED", "CPR Mask", "Nitrile Gloves (pair)"]
    assert len(top.instructions) >= 4
    assert any("QR-02" in ref.source for ref in top.source_chunks)
    assert "matched" in top.reasoning


def test_unrelated_text_produces_no_diagnosis():
    """One incidental word is evidence, but not enough to offer a diagnosis.

    ``match`` reports the raw hit so it stays debuggable; ``situations`` applies
    the score floor, and that is what reaches the manager.
    """
    weak = rule_engine.match("the generator paperwork was filed yesterday")
    assert [m.rule.id for m in weak] == ["power-lighting"]
    assert weak[0].score == 1

    assert rule_engine.situations("the generator paperwork was filed yesterday") == []
    assert rule_engine.situations("nothing at all happened here today") == []


def test_hypotheses_are_grouped_by_severity():
    buckets = rule_engine.hypotheses("he is not moving and his legs look wrong")
    assert buckets
    assert set(buckets).issubset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


def test_normalisation_handles_apostrophes_and_accents():
    assert normalise("Can't breathe!") == "cant breathe"
    assert normalise("  Ambulancé   ARRIVED ") == "ambulance arrived"
    assert keyword_hits("she cannot breathe properly", ["cannot breathe"]) == ["cannot breathe"]
    # Whole-phrase matching only — no accidental substring hits.
    assert keyword_hits("reprogrammed the radio", ["cpr"]) == []


def test_fuzzy_match_refuses_weak_candidates():
    names = ["AED", "CPR Mask", "Glucose Tablets", "Nitrile Gloves (pair)"]
    assert fuzzy_best_match("cpr mask", names)[1] == "CPR Mask"
    assert fuzzy_best_match("gloves", names)[1] == "Nitrile Gloves (pair)"
    assert fuzzy_best_match("ventilator", names) is None
    assert similarity("AED", "AED") == 100.0


@pytest.mark.parametrize(
    "report,expected_id",
    [
        ("Family of four needs water and blankets", "supply-request"),
        ("she needs food, two children have not eaten", "supply-request"),
        ("several people are vomiting after eating", "contamination"),
        ("two teeth were knocked out", "dental-jaw"),
        ("there are burns on both arms", "burns"),
        ("his legs look wrong and he cannot walk", "fracture"),
    ],
)
def test_plural_and_verb_forms_still_match(report, expected_id):
    """Reports say "needs blankets"; rules say "need blanket"."""
    situations = rule_engine.situations(report)
    assert situations, f"no diagnosis for {report!r}"
    assert _top(report).rule.id == expected_id


def test_stemming_does_not_mangle_non_plurals():
    from aria.utils.textutil import stem_text

    assert stem_text("blankets needs legs") == "blanket need leg"
    # Words that merely end in s must survive intact.
    assert stem_text("gas status crisis unconscious") == "gas status crisis unconscious"
