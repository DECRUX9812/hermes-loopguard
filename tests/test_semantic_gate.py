from __future__ import annotations

import pytest

from hermes_loopguard.backends.base import Backend, BackendError, GateResult, Usage
from hermes_loopguard.questions import ChoiceAnswer, NoulAnswer, ScoreAnswer
from hermes_loopguard.semantic_gate import (
    GateDecision,
    check_condition,
    decide,
    evaluate,
    gate_spec_from_dict,
)


def test_check_condition():
    assert check_condition(0.7, ">=0.6") is True
    assert check_condition(0.5, ">=0.6") is False
    assert check_condition(1.0, "<=1") is True
    assert check_condition(2.0, "==2") is True
    assert check_condition(2.1, "==2") is False
    assert check_condition(0.2, "<0.3") is True
    assert check_condition(0.4, ">0.3") is True


def test_gate_spec_and_decide_admit():
    d = {
        "questions": {
            "kind": {
                "type": "choice",
                "instructions": "Kind of issue",
                "criteria": {"bug": "Defect", "feature": "Request"},
            },
            "actionable": {
                "type": "noul",
                "instructions": "Is actionable?",
            },
        },
        "route_question": "kind",
        "routes": {
            "bug": {
                "handler": "agent",
                "min_prob": 0.6,
                "when": {"actionable": ">=0.7"},
                "brief": "Fix bug carefully",
            },
            "feature": {"handler": "review", "min_prob": 0.5},
        },
        "default_route": "review",
    }
    spec = gate_spec_from_dict(d)

    answers = {
        "kind": ChoiceAnswer(choice="bug", confidence=0.8, probabilities={"bug": 0.8, "feature": 0.2}),
        "actionable": NoulAnswer(noul=0.9),
    }
    route, reasons = decide(spec, answers)
    assert route == "bug"
    assert "admitted to route 'bug'" in reasons[-1]


def test_decide_low_prob_fallback():
    d = {
        "questions": {
            "kind": {
                "type": "choice",
                "instructions": "Kind",
                "criteria": {"bug": "Defect"},
            }
        },
        "route_question": "kind",
        "routes": {"bug": {"handler": "agent", "min_prob": 0.7}},
        "default_route": "review",
    }
    spec = gate_spec_from_dict(d)
    answers = {
        "kind": ChoiceAnswer(choice="bug", confidence=0.5, probabilities={"bug": 0.5}),
    }
    route, reasons = decide(spec, answers)
    assert route == "review"
    assert "below min_prob" in reasons[-1]


def test_decide_when_condition_fails():
    d = {
        "questions": {
            "kind": {"type": "choice", "instructions": "Kind", "criteria": {"bug": "Defect"}},
            "actionable": {"type": "noul", "instructions": "Actionable?"},
        },
        "route_question": "kind",
        "routes": {
            "bug": {"handler": "agent", "min_prob": 0.6, "when": {"actionable": ">=0.8"}},
        },
        "default_route": "review",
    }
    spec = gate_spec_from_dict(d)
    answers = {
        "kind": ChoiceAnswer(choice="bug", confidence=0.9, probabilities={"bug": 0.9}),
        "actionable": NoulAnswer(noul=0.5),
    }
    route, reasons = decide(spec, answers)
    assert route == "review"
    assert "condition 'actionable >=0.8' failed" in reasons[-1]


class FakeBackend:
    # FAKE BACKEND (unit test only)
    name = "fake"

    def __init__(self, canned_answers=None, should_raise=False):
        self.canned_answers = canned_answers or {}
        self.should_raise = should_raise

    def available(self):
        return True

    def ask(self, state, questions):
        if self.should_raise:
            raise BackendError("Fake backend network down")
        return GateResult(
            answers=self.canned_answers,
            backend=self.name,
            model="fake-v1",
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.0001),
        )


def test_evaluate_success_and_error():
    d = {
        "questions": {
            "kind": {"type": "choice", "instructions": "Kind", "criteria": {"bug": "Defect"}},
        },
        "route_question": "kind",
        "routes": {"bug": {"handler": "agent", "min_prob": 0.6}},
        "default_route": "review",
    }
    spec = gate_spec_from_dict(d)

    answers = {"kind": ChoiceAnswer(choice="bug", confidence=0.8, probabilities={"bug": 0.8})}
    chain = [FakeBackend(canned_answers=answers)]

    dec = evaluate(spec, {"title": "broken"}, chain)
    assert dec.route == "bug"
    assert dec.handler == "agent"
    assert dec.backend == "fake"
    assert dec.prob == 0.8

    # When backend raises error
    failing_chain = [FakeBackend(should_raise=True)]
    dec_err = evaluate(spec, {"title": "broken"}, failing_chain)
    assert dec_err.route == "review"
    assert dec_err.handler == "review"
    assert dec_err.error is not None
    assert "Backend error" in dec_err.reasons[0]
