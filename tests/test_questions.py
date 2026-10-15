from __future__ import annotations

import pytest

from hermes_loopguard.questions import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    answer_from_dict,
    answer_to_dict,
    choice_answer,
    normalize,
    question_from_spec,
    question_to_dict,
    questions_from_spec,
    questions_version,
    score_answer,
)


def test_question_spec_and_version():
    spec = {
        "kind": {
            "type": "choice",
            "instructions": "Select issue kind",
            "criteria": {"bug": "Defect", "feature": "New feature"},
        },
        "actionable": {
            "type": "noul",
            "instructions": "Is it actionable?",
            "criteria": {"true": "Clear steps", "false": "Vague"},
        },
        "urgency": {
            "type": "score",
            "instructions": "Rate urgency",
            "criteria": ["low", "medium", "critical"],
        },
    }
    qs = questions_from_spec(spec)
    assert isinstance(qs["kind"], Choice)
    assert isinstance(qs["actionable"], Noul)
    assert isinstance(qs["urgency"], Score)

    v1 = questions_version(qs)
    assert v1.startswith("q-")
    assert len(v1) == 14  # 'q-' + 12 chars

    # Determinism
    v2 = questions_version(qs)
    assert v1 == v2

    d = question_to_dict(qs["kind"])
    assert d["type"] == "choice"
    assert d["instructions"] == "Select issue kind"


def test_invalid_question_specs():
    with pytest.raises(ValueError):
        question_from_spec({"type": "choice", "instructions": ""})
    with pytest.raises(ValueError):
        question_from_spec({"type": "unknown", "instructions": "test"})
    with pytest.raises(ValueError):
        question_from_spec({"type": "score", "instructions": "test", "criteria": "not a list"})


def test_normalize():
    assert normalize({}) == {}
    assert normalize({"a": 0, "b": 0}) == {"a": 0.5, "b": 0.5}
    norm = normalize({"a": 1, "b": 3})
    assert pytest.approx(norm["a"]) == 0.25
    assert pytest.approx(norm["b"]) == 0.75


def test_choice_and_score_answers():
    ca = choice_answer({"bug": 0.8, "feature": 0.2})
    assert ca.choice == "bug"
    assert pytest.approx(ca.confidence) == 0.8

    sa = score_answer({"0": 0.1, "1": 0.7, "2": 0.2}, ["low", "medium", "high"])
    assert sa.confidence == pytest.approx(0.7)
    assert sa.score == pytest.approx(0 * 0.1 + 1 * 0.7 + 2 * 0.2)
    assert sa.legend == {0: "low", 1: "medium", 2: "high"}


def test_answer_serialization():
    ca = ChoiceAnswer(choice="bug", confidence=0.9, probabilities={"bug": 0.9, "feature": 0.1})
    d_ca = answer_to_dict(ca)
    ca_back = answer_from_dict(d_ca)
    assert isinstance(ca_back, ChoiceAnswer)
    assert ca_back.choice == "bug"
    assert ca_back.confidence == 0.9

    na = NoulAnswer(noul=0.85)
    d_na = answer_to_dict(na)
    na_back = answer_from_dict(d_na)
    assert isinstance(na_back, NoulAnswer)
    assert pytest.approx(na_back.noul) == 0.85

    sa = ScoreAnswer(score=1.5, confidence=0.6, probabilities={0: 0.1, 1: 0.6, 2: 0.3}, legend={0: "a", 1: "b", 2: "c"})
    d_sa = answer_to_dict(sa)
    sa_back = answer_from_dict(d_sa)
    assert isinstance(sa_back, ScoreAnswer)
    assert pytest.approx(sa_back.score) == 1.5
    assert sa_back.legend[1] == "b"
