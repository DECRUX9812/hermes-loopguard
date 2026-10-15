from __future__ import annotations

import json
import sys
import types

import pytest

from hermes_loopguard.backends import ask_with_fallback, backend_chain, make_backend
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.backends.local import LocalBackend
from hermes_loopguard.backends.openai_compat import OpenAICompatBackend
from hermes_loopguard.backends.typesafe import TypeSafeBackend
from hermes_loopguard.questions import Choice, Noul, Score


def test_local_backend():
    cfg = {
        "local": {
            "keywords": {
                "bug": ["crash", "error", "traceback"],
                "actionable": {"yes": ["steps", "reproduce"], "no": ["vague", "help"]},
            }
        }
    }
    backend = LocalBackend(cfg)
    assert backend.available() is True

    qs = {
        "kind": Choice(instructions="Category", criteria={"bug": "defect", "feature": "request"}),
        "actionable": Noul(instructions="Is it actionable?"),
        "urgency": Score(instructions="Urgency level", criteria=["low", "medium", "high"]),
    }

    state = {"title": "App crashes with error traceback", "body": "Steps to reproduce: run command"}
    res = backend.ask(state, qs)
    assert res.backend == "local"
    assert res.model == "heuristic-v1"
    assert res.usage.cost_usd == 0.0

    assert "kind" in res.answers
    assert res.answers["kind"].choice == "bug"
    assert res.answers["kind"].probabilities["bug"] > res.answers["kind"].probabilities["feature"]

    assert "actionable" in res.answers
    assert res.answers["actionable"].noul > 0.5

    assert "urgency" in res.answers
    assert res.answers["urgency"].score == 0.0
    assert pytest.approx(res.answers["urgency"].probabilities[0]) == 1.0 / 3.0


def test_local_backend_uniform_when_no_hits():
    backend = LocalBackend({})
    qs = {"kind": Choice(instructions="Cat", criteria={"x": None, "y": None})}
    res = backend.ask({"msg": "nothing relevant"}, qs)
    assert pytest.approx(res.answers["kind"].probabilities["x"]) == 0.5
    assert pytest.approx(res.answers["kind"].probabilities["y"]) == 0.5


def test_openai_compat_backend_json_mode(monkeypatch):
    # FAKE BACKEND (unit test only)
    cfg = {
        "openai": {
            "base_url": "http://mock-llm.local/v1",
            "model": "mock-gpt",
            "api_key_env": "OPENAI_API_KEY",
            "usd_per_mtok_in": 1.5,
            "usd_per_mtok_out": 2.0,
        }
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-mock")

    backend = OpenAICompatBackend(cfg)
    assert backend.available() is True

    canned_reply = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "kind": {"probabilities": {"bug": 0.85, "feature": 0.15}},
                            "actionable": {"yes": 0.95},
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    def fake_post(payload):
        # FAKE BACKEND (unit test only)
        return canned_reply

    monkeypatch.setattr(backend, "_post", fake_post)

    qs = {
        "kind": Choice(instructions="Category", criteria={"bug": "defect", "feature": "request"}),
        "actionable": Noul(instructions="Actionable?"),
    }
    res = backend.ask({"title": "login bug"}, qs)
    assert res.answers["kind"].choice == "bug"
    assert pytest.approx(res.answers["kind"].confidence) == 0.85
    assert pytest.approx(res.answers["actionable"].noul) == 0.95
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 50
    # 100 * 1.5 / 1e6 + 50 * 2.0 / 1e6 = 0.00015 + 0.00010 = 0.00025
    assert pytest.approx(res.usage.cost_usd) == 0.00025


class RaisingBackend:
    # FAKE BACKEND (unit test only)
    name = "raising_backend"

    def available(self):
        return True

    def ask(self, state, questions):
        raise BackendError("Backend connection failed")


def test_fallback_chain_skips_raising_backend():
    # FAKE BACKEND (unit test only)
    failing = RaisingBackend()
    working = LocalBackend({})
    chain = [failing, working]

    qs = {"kind": Choice(instructions="Cat", criteria={"a": None, "b": None})}
    res = ask_with_fallback(chain, {"title": "item"}, qs)
    assert res.backend == "local"
    assert "raising_backend" in res.error

    # When all fail
    with pytest.raises(BackendError) as exc_info:
        ask_with_fallback([RaisingBackend()], {"title": "item"}, qs)
    assert "All backends in chain failed" in str(exc_info.value)


# --- typesafe backend (fake module mirrors the real typesafe-sdk surface) ---


class _FakeTypeSafeError(Exception):
    pass


class _FakeTypeSafeAPIError(_FakeTypeSafeError):
    def __init__(self, message, status=500, request_id="req-fake-1"):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class _FakeChoice:
    type = "choice"

    def __init__(self, instructions, criteria=None):
        self.instructions = instructions
        self.criteria = criteria


class _FakeNoul:
    type = "noul"

    def __init__(self, instructions, criteria=None):
        self.instructions = instructions
        self.criteria = criteria


class _FakeScore:
    type = "score"

    def __init__(self, instructions, criteria=None):
        self.instructions = instructions
        self.criteria = criteria


class _FakeChoiceAnswer:
    type = "choice"

    def __init__(self, choice, confidence, probabilities):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities


class _FakeNoulAnswer:
    type = "noul"

    def __init__(self, noul):
        self.noul = noul


class _FakeScoreAnswer:
    type = "score"

    def __init__(self, score, confidence, legend, probabilities):
        self.score = score
        self.confidence = confidence
        self.legend = legend
        self.probabilities = probabilities


class _FakeUsage:
    def __init__(self, input_tokens=None, output_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, model, usage, answers):
        self.model = model
        self.usage = usage
        self.answers = answers


class _FakeClient:
    # FAKE BACKEND (unit test only) — mirrors typesafe_sdk.TypeSafeClient:
    # keyword-only constructor, context manager, system_one(state, questions).
    def __init__(self, response=None, error=None, **kwargs):
        self.response = response
        self.error = error
        self.init_kwargs = kwargs
        self.last_state = None
        self.last_questions = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def system_one(self, state, questions):
        self.last_state = state
        self.last_questions = questions
        if self.error is not None:
            raise self.error
        return self.response


def _install_fake_typesafe_sdk(monkeypatch, response=None, error=None, clients=None):
    # FAKE BACKEND (unit test only) — a typesafe_sdk module mirroring the
    # real 0.7.x surface the backend was written against.
    module = types.ModuleType("typesafe_sdk")
    module.Choice = _FakeChoice
    module.Noul = _FakeNoul
    module.Score = _FakeScore
    module.ChoiceAnswer = _FakeChoiceAnswer
    module.NoulAnswer = _FakeNoulAnswer
    module.ScoreAnswer = _FakeScoreAnswer
    module.TypeSafeError = _FakeTypeSafeError
    module.TypeSafeAPIError = _FakeTypeSafeAPIError

    def _client_factory(**kwargs):
        client = _FakeClient(response=response, error=error, **kwargs)
        if clients is not None:
            clients.append(client)
        return client

    module.TypeSafeClient = _client_factory
    monkeypatch.setitem(sys.modules, "typesafe_sdk", module)
    return module


TEST_QUESTIONS = {
    "kind": Choice(instructions="Category", criteria={"bug": "defect", "feature": "request"}),
    "actionable": Noul(
        instructions="Actionable?", criteria={"true": "enough detail", "false": "too vague"}
    ),
    "urgency": Score(instructions="Urgency", criteria=["low", "medium", "high"]),
}


def test_typesafe_backend_maps_answers_usage_and_questions(monkeypatch):
    response = _FakeResponse(
        model="jev-1.13.0",
        usage=_FakeUsage(input_tokens=428, output_tokens=73),
        answers={
            "kind": _FakeChoiceAnswer(
                choice="bug", confidence=0.9, probabilities={"bug": 0.9, "feature": 0.1}
            ),
            "actionable": _FakeNoulAnswer(noul=0.85),
            "urgency": _FakeScoreAnswer(
                score=1.2,
                confidence=0.8,
                legend={0: "low", 1: "medium", 2: "high"},
                probabilities={0: 0.3, 1: 0.6, 2: 0.1},
            ),
        },
    )
    clients: list[_FakeClient] = []
    _install_fake_typesafe_sdk(monkeypatch, response=response, clients=clients)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    cfg = {"typesafe": {"model": "jev-latest", "timeout": 7,
                        "usd_per_mtok_in": 1.0, "usd_per_mtok_out": 2.0}}
    backend = TypeSafeBackend(cfg)
    assert backend.available() is True

    res = backend.ask({"title": "cart crash"}, TEST_QUESTIONS)
    assert res.backend == "typesafe"
    assert res.model == "jev-1.13.0"

    assert res.answers["kind"].choice == "bug"
    assert res.answers["kind"].confidence == pytest.approx(0.9)
    assert res.answers["kind"].probabilities == {"bug": pytest.approx(0.9),
                                                 "feature": pytest.approx(0.1)}
    assert res.answers["actionable"].noul == pytest.approx(0.85)
    assert res.answers["urgency"].score == pytest.approx(1.2)
    assert res.answers["urgency"].legend == {0: "low", 1: "medium", 2: "high"}
    assert res.answers["urgency"].probabilities[1] == pytest.approx(0.6)

    assert res.usage.input_tokens == 428
    assert res.usage.output_tokens == 73
    # 428 * 1.0 / 1e6 + 73 * 2.0 / 1e6
    assert res.usage.cost_usd == pytest.approx(0.000574)

    assert len(clients) == 1
    client = clients[0]
    assert client.init_kwargs == {"api_key": "fake-key-for-tests", "model": "jev-latest",
                                  "timeout": 7.0}
    assert client.last_state == {"title": "cart crash"}
    # our Question objects were forwarded as SDK question objects
    assert isinstance(client.last_questions["kind"], _FakeChoice)
    assert isinstance(client.last_questions["actionable"], _FakeNoul)
    assert isinstance(client.last_questions["urgency"], _FakeScore)


def test_typesafe_backend_usage_defaults_when_unreported(monkeypatch):
    response = _FakeResponse(
        model="jev-latest",
        usage=_FakeUsage(input_tokens=None, output_tokens=None),
        answers={"kind": _FakeChoiceAnswer(choice="bug", confidence=0.8,
                                           probabilities={"bug": 0.8, "feature": 0.2})},
    )
    _install_fake_typesafe_sdk(monkeypatch, response=response)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    backend = TypeSafeBackend({})
    res = backend.ask({"title": "t"}, {"kind": TEST_QUESTIONS["kind"]})
    assert res.usage.input_tokens == 0
    assert res.usage.output_tokens == 0
    assert res.usage.cost_usd == 0.0
    assert res.answers["kind"].choice == "bug"


def test_typesafe_backend_missing_key(monkeypatch):
    _install_fake_typesafe_sdk(monkeypatch)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    backend = TypeSafeBackend({})
    assert backend.available() is False
    with pytest.raises(BackendError) as exc_info:
        backend.ask({"title": "t"}, TEST_QUESTIONS)
    assert "Missing API key" in str(exc_info.value)


def test_typesafe_backend_sdk_error(monkeypatch):
    _install_fake_typesafe_sdk(
        monkeypatch, error=_FakeTypeSafeAPIError("rate limited", status=429)
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    backend = TypeSafeBackend({})
    with pytest.raises(BackendError) as exc_info:
        backend.ask({"title": "t"}, TEST_QUESTIONS)
    assert "TypeSafe System One call failed" in str(exc_info.value)
    assert "rate limited" in str(exc_info.value)


def test_typesafe_backend_sdk_not_installed(monkeypatch):
    # sys.modules["typesafe_sdk"] = None makes `import typesafe_sdk` raise
    # ImportError even if the real package were importable.
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    backend = TypeSafeBackend({})
    assert backend.available() is False
    with pytest.raises(BackendError) as exc_info:
        backend.ask({"title": "t"}, TEST_QUESTIONS)
    assert "typesafe-sdk is not installed" in str(exc_info.value)
