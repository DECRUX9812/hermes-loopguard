"""Tests for the eval harness against the example JSONL and a fake backend."""
import json
from pathlib import Path

import pytest

evals = pytest.importorskip("hermes_loopguard.evals")

EXAMPLE_CASES = Path(__file__).resolve().parent.parent / "examples" / "eval" / "issue_triage_cases.jsonl"
VALID_ROUTES = {"bug", "feature", "question", "duplicate_or_spam", "review"}

# GateSpec dict mirroring examples/issue_triage.yaml (kept inline so the test
# does not need pyyaml).
GATE_SPEC = {
    "route_question": "kind",
    "questions": {
        "kind": {"type": "choice", "instructions": "What does this issue need?",
                 "criteria": {"bug": "a defect in existing behaviour",
                              "feature": "a request for new behaviour",
                              "question": "a usage question",
                              "duplicate_or_spam": "spam, empty, duplicate, or off-topic"}},
        "actionable": {"type": "noul", "instructions": "enough detail to start now?"},
        "urgency": {"type": "score", "instructions": "How urgent?",
                    "criteria": ["can wait", "needs attention this week",
                                 "users are blocked or data is at risk"]},
    },
    "routes": {
        "bug": {"handler": "agent", "min_prob": 0.6, "when": {"actionable": ">=0.6"}},
        "feature": {"handler": "review", "min_prob": 0.5},
        "question": {"handler": "review", "min_prob": 0.5},
        "duplicate_or_spam": {"handler": "drop", "min_prob": 0.75},
    },
    "default_route": "review",
}


class FakeBackend:  # FAKE BACKEND (unit test only)
    name = "fake"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def ask(self, state, questions):
        self.calls += 1
        from hermes_loopguard.questions import NoulAnswer, choice_answer, score_answer
        from hermes_loopguard.backends.base import GateResult, Usage
        answers = {}
        for name, question in questions.items():
            qtype = getattr(question, "type", "choice")
            if qtype == "choice":
                answers[name] = choice_answer({"bug": 0.9, "feature": 0.04,
                                               "question": 0.03, "duplicate_or_spam": 0.03})
            elif qtype == "noul":
                answers[name] = NoulAnswer(noul=0.9)
            else:
                levels = list(getattr(question, "criteria", []) or ["l0", "l1", "l2"])
                answers[name] = score_answer({0: 0.1, 1: 0.2, 2: 0.7}, levels)
        return GateResult(answers=answers, backend="fake", model="fake-1",
                          usage=Usage(input_tokens=10, output_tokens=5))


def make_spec():
    semantic_gate = pytest.importorskip("hermes_loopguard.semantic_gate")
    return semantic_gate.gate_spec_from_dict(GATE_SPEC)


def test_load_example_cases():
    cases = evals.load_cases(EXAMPLE_CASES)
    assert len(cases) == 40
    assert len({c.id for c in cases}) == 40
    for case in cases:
        assert case.expected_route in VALID_ROUTES, case.id
        assert isinstance(case.state, dict), case.id
        assert "title" in case.state and "body" in case.state and "labels" in case.state
    by_route = {}
    for case in cases:
        by_route[case.expected_route] = by_route.get(case.expected_route, 0) + 1
    assert by_route == {"bug": 12, "review": 6, "feature": 8, "question": 7,
                        "duplicate_or_spam": 7}


def test_run_eval_counts():
    cases = evals.load_cases(EXAMPLE_CASES)
    spec = make_spec()
    backend = FakeBackend()
    report = evals.run_eval(spec, cases, [backend])

    assert backend.calls == 40
    assert report.n == 40
    assert all(row.predicted_route == "bug" for row in report.rows)
    # every predicted route admits (agent), so no misses
    assert report.misses == []
    # 28 cases expect a non-admitting route (6 review + 8 feature + 7 question + 7 drop)
    assert len(report.false_admits) == 28
    assert report.accuracy == pytest.approx(12 / 40)
    assert report.confusion["bug"]["bug"] == 12
    assert report.confusion["review"]["bug"] == 6
    assert report.usage["calls"] == 40
    assert report.usage["input_tokens"] == 400
    assert report.usage["output_tokens"] == 200
    assert report.sweep  # run_eval populates a default sweep


def test_threshold_sweep_no_extra_backend_calls():
    cases = evals.load_cases(EXAMPLE_CASES)
    spec = make_spec()
    backend = FakeBackend()
    report = evals.run_eval(spec, cases, [backend])
    calls_after_eval = backend.calls

    sweep = evals.threshold_sweep(spec, report.rows, thresholds=(0.3, 0.95))
    assert backend.calls == calls_after_eval  # re-decided from cached answers

    at_03 = next(e for e in sweep if e["threshold"] == 0.3)
    assert at_03["misses"] == 0 and at_03["false_admits"] == 28
    at_095 = next(e for e in sweep if e["threshold"] == 0.95)
    # prob 0.9 < 0.95: nothing admits, so the 12 expected bugs become misses
    assert at_095["misses"] == 12 and at_095["false_admits"] == 0


def test_format_report():
    cases = evals.load_cases(EXAMPLE_CASES)
    spec = make_spec()
    report = evals.run_eval(spec, cases, [FakeBackend()])
    text = evals.format_report(report)
    assert "accuracy" in text
    assert "c13" in text  # a false admit: vague bug, predicted bug
    assert "0.9" in text  # sweep table lists default thresholds
    assert "Confusion matrix" in text
    assert "False admits" in text
    # sanity: the report round-trips through nothing, it is plain text
    assert isinstance(text, str)


def test_load_cases_ignores_blank_lines(tmp_path):
    path = tmp_path / "cases.jsonl"
    case = {"id": "x1", "state": {"title": "t"}, "expected_route": "review"}
    path.write_text(json.dumps(case) + "\n\n" + json.dumps({**case, "id": "x2"}) + "\n")
    cases = evals.load_cases(path)
    assert [c.id for c in cases] == ["x1", "x2"]
